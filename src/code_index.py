import argparse
import json
import sys
import os
import hashlib
import subprocess
import ast
import fcntl
from contextlib import contextmanager
from pathlib import Path
import psycopg

import tree_sitter_python as tspython
import tree_sitter_bash as tsbash
from tree_sitter import Language, Parser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, project_info
from repo_identity import validate_repo_name, reject_flag_like, InvalidRepoIdentity  # noqa: F401 (InvalidRepoIdentity re-exported as code_index.InvalidRepoIdentity for callers)
import delegate
from rules_index import get_model


def _project_info_for(repo_path: Path):
    """Repo identity for the multi-repo sweep: always the directory's own
    basename -- never the git remote URL.

    The remote URL is not a safe identity source here: two different
    checkouts can point at the same remote (e.g. parsi-rtl-test's origin is
    a stale copy of parsi-rtl's) and would then collide under one
    code_chunks.repo value, with each ingest's --force GC step deleting the
    other's chunks (T-953 defect: parsi-rtl-test missing entirely).
    get_repo_roots() already guarantees each swept root is a distinct
    sibling directory, so its basename is unique and stable -- and it
    matches the casing people actually use (was "arix" from the lowercase
    remote vs the real "Arix" directory).
    """
    def git(*a):
        try:
            r = subprocess.run(["git", *a], cwd=repo_path, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""
    return (repo_path.name, git("rev-parse", "--short", "HEAD") or None)

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_chunks (
                id bigserial PRIMARY KEY,
                repo text,
                path text,
                lang text,
                symbol text,
                parent_symbol text,
                start_line int,
                end_line int,
                chunk text,
                chunk_hash text,
                repo_commit text,
                embedding vector(384)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx 
            ON code_chunks USING hnsw (embedding vector_cosine_ops);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_edges (
                caller_id bigint REFERENCES code_chunks(id) ON DELETE CASCADE,
                callee_symbol text,
                resolved_id bigint REFERENCES code_chunks(id) ON DELETE SET NULL,
                PRIMARY KEY (caller_id, callee_symbol)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingested_files (
                collection text,
                file_path text,
                content_hash text,
                updated_at timestamp DEFAULT current_timestamp,
                PRIMARY KEY (collection, file_path)
            );
        """)
    conn.commit()


def _default_repo_roots() -> list[Path]:
    """Every git checkout directly under $HOME/@-github/ (the DoD default)."""
    base = Path.home() / "@-github"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / ".git").exists())


def get_repo_roots() -> list[Path]:
    """Repo roots to sweep, read from <vault>/data/code_repo_roots.json.

    Adding/removing a repo is a config edit, not a commit. Accepts either
    {"roots": ["/abs/path", ...]} or a bare JSON list of path strings.
    Missing file, unreadable file, or invalid JSON all fall back to
    _default_repo_roots() (logged to stderr on a parse/read error, silent
    on a simply-missing file — a missing config is the normal/default case).
    Non-existent paths in the config are silently dropped.
    """
    cfg_path = delegate.DATA_DIR / "code_repo_roots.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text("utf-8"))
            roots = data["roots"] if isinstance(data, dict) else data
            paths = [Path(r).expanduser() for r in roots]
            return [p for p in paths if p.is_dir()]
        except Exception as e:
            print(f"code_index: bad {cfg_path}, falling back to default roots: {e}", file=sys.stderr)
    return _default_repo_roots()


_EXCLUDE_DIR_PARTS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".git"}


def _is_excluded(rel_path: str) -> bool:
    """Vendored/generated paths to skip even if git-tracked (defense in depth)."""
    parts = Path(rel_path).parts
    if any(part in _EXCLUDE_DIR_PARTS for part in parts):
        return True
    return rel_path.endswith(".min.js")


# Extensions this indexer treats as "code" (T-953 scope: .py .js .ts .tsx
# .jsx .sh .go .rs). Only .py and .sh get real AST-based chunking, via the
# tree-sitter grammars vendored in this repo (tree_sitter_python,
# tree_sitter_bash). Grammars for the rest (tree-sitter-javascript /
# -typescript / -go / -rust) would be new dependencies needing owner
# approval, so those extensions fall back to chunk_generic()'s coarser
# whole-file splitting instead of AST-aware function/class chunks. That
# still makes the file searchable, which it was not at all before this fix
# (T-953 defects #1/#2: portfolio, parsi-rtl, parsi-rtl-test are 100%
# js/ts and were silently skipped in full).
CODE_EXT_LANG = {
    ".py": "python",
    ".sh": "bash",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}
_TREE_SITTER_EXTS = {".py", ".sh"}
CODE_GLOBS = tuple(f"*{ext}" for ext in CODE_EXT_LANG)


def _lang_for_path(rel_path: str) -> str | None:
    """The CODE_EXT_LANG language tag for a file, or None if its extension
    is not indexed as code at all."""
    return CODE_EXT_LANG.get(Path(rel_path).suffix)

# Tree-sitter parsers
PY_LANG = Language(tspython.language())
BASH_LANG = Language(tsbash.language())

def get_parser(lang):
    p = Parser(lang)
    return p

def get_signature(node, source_bytes):
    # Extracts everything up to the body
    body = node.child_by_field_name('body')
    if not body:
        return source_bytes[node.start_byte:node.end_byte].decode('utf-8')
    sig_bytes = source_bytes[node.start_byte:body.start_byte]
    return sig_bytes.decode('utf-8').strip()

def chunk_node(root_node, lang, tokenizer, source_bytes, parent_symbol=None):
    # Iterative pre-order walk over `node.children` lists only. The recursive
    # cursor/next_sibling walk deterministically segfaulted py-tree-sitter
    # 0.26.0 on macOS with real-size files; materialized children are stable.
    chunks = []

    def emit(node, current_symbol, current_parent_symbol):
        text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
        tokens = len(text) // 3

        if tokens > 400:
            body = node.child_by_field_name('body')
            if body:
                sig = get_signature(node, source_bytes)
                body_children = body.children
                current_subchunk_text = ""
                current_start = -1

                for i, child in enumerate(body_children):
                    child_text = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                    temp = current_subchunk_text + "\n" + child_text if current_subchunk_text else child_text
                    if (len(sig) + len(temp)) // 3 > 400:
                        if current_subchunk_text:
                            prev_child = body_children[i - 1] if i > 0 else None
                            chunks.append({
                                "symbol": current_symbol,
                                "parent_symbol": current_parent_symbol,
                                "start_line": current_start + 1,
                                "end_line": prev_child.end_point.row + 1 if prev_child else current_start + 1,
                                "text": sig + "\n" + current_subchunk_text.strip()
                            })
                        current_subchunk_text = child_text
                        current_start = child.start_point.row
                    else:
                        if not current_subchunk_text:
                            current_start = child.start_point.row
                        current_subchunk_text = temp

                if current_subchunk_text:
                    chunks.append({
                        "symbol": current_symbol,
                        "parent_symbol": current_parent_symbol,
                        "start_line": current_start + 1,
                        "end_line": body_children[-1].end_point.row + 1 if body_children else current_start + 1,
                        "text": sig + "\n" + current_subchunk_text.strip()
                    })
                return

        chunks.append({
            "symbol": current_symbol,
            "parent_symbol": current_parent_symbol,
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "text": text
        })

    stack = [(root_node, parent_symbol)]
    while stack:
        node, current_parent_symbol = stack.pop()

        symbol_name = None
        if node.type in ('function_definition', 'class_definition'):
            name_node = node.child_by_field_name('name')
            if name_node:
                symbol_name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')

        current_symbol = symbol_name if symbol_name else current_parent_symbol
        if current_parent_symbol and symbol_name:
            current_symbol = f"{current_parent_symbol}.{symbol_name}"

        if node.type in ('function_definition', 'class_definition'):
            emit(node, current_symbol, current_parent_symbol)

        next_parent = current_symbol if node.type in ('class_definition', 'function_definition') else current_parent_symbol
        for child in reversed(node.children):
            stack.append((child, next_parent))

    return chunks

def chunk_generic(source_bytes: bytes) -> list[dict]:
    """Whole-file chunking for languages with no tree-sitter grammar in this
    repo (js/jsx/ts/tsx/go/rust). No symbol/parent_symbol extraction --
    coarser than the AST-based python/bash path, but it makes the file
    searchable at all, which it was not before. Splits on ~400-token
    (chars//3) line boundaries so a large file still yields more than one
    embeddable chunk.
    """
    text = source_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return []
    chunks = []
    buf = []
    start = 1
    for i, line in enumerate(lines, start=1):
        buf.append(line)
        if len("\n".join(buf)) // 3 > 400:
            chunks.append({
                "symbol": None, "parent_symbol": None,
                "start_line": start, "end_line": i,
                "text": "\n".join(buf),
            })
            buf = []
            start = i + 1
    if buf:
        chunks.append({
            "symbol": None, "parent_symbol": None,
            "start_line": start, "end_line": len(lines),
            "text": "\n".join(buf),
        })
    return chunks


def cmd_chunk_files(paths):
    """Chunk the given files and print {path: [chunk, ...]} as JSON.

    Runs as a dedicated child process that only ever touches tree-sitter:
    live tokenizers/onnxruntime objects in the same process corrupt
    tree-sitter walks on macOS arm64 (deterministic segfault — see
    docs/CODE-RAG.md), so the child never creates them and the parent
    (embedding/DB side) never parses.
    """
    out = {}
    for rel in paths:
        ext = Path(rel).suffix
        try:
            source_bytes = Path(rel).read_bytes()
            if ext in _TREE_SITTER_EXTS:
                lang = CODE_EXT_LANG[ext]
                parser = get_parser(PY_LANG if ext == '.py' else BASH_LANG)
                tree = parser.parse(source_bytes)
                chunks = [c for c in chunk_node(tree.root_node, lang, None, source_bytes) if c['text'].strip()]
                if not chunks:
                    # No function_definition/class_definition node in the
                    # file (a top-level script, not a library of defs) --
                    # fall back to the same whole-file chunker used for
                    # languages with no tree-sitter grammar here, so real
                    # top-level code is still searchable instead of
                    # contributing 0 chunks (T-953 follow-up: this is what
                    # was silently swallowing e.g. polycast's
                    # experiments/gemini/scripts/*.py). A file with 0 bytes
                    # of real content still yields 0 chunks either way --
                    # chunk_generic() returns [] for an empty file.
                    chunks = [c for c in chunk_generic(source_bytes) if c['text'].strip()]
            else:
                chunks = [c for c in chunk_generic(source_bytes) if c['text'].strip()]
        except Exception as e:
            print(f"Failed to process {rel}: {e}", file=sys.stderr)
            continue
        out[rel] = chunks
    print(json.dumps(out))


def _chunk_files_subprocess(paths):
    """Chunk files via one `chunk-files` child process; returns {path: chunks}."""
    if not paths:
        return {}
    root = Path(__file__).resolve().parent.parent
    res = subprocess.run(
        [sys.executable, "-m", "src.code_index", "chunk-files", *[str(p) for p in paths]],
        cwd=root, capture_output=True, text=True, check=True,
    )
    if res.stderr:
        print(res.stderr, file=sys.stderr, end="")
    return json.loads(res.stdout)


def extract_python_calls(source):
    calls = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return calls
    
    current_class = None
    current_func = None
    
    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            nonlocal current_class, current_func
            prev_class = current_class
            current_class = node.name
            self.generic_visit(node)
            current_class = prev_class

        def visit_FunctionDef(self, node):
            nonlocal current_class, current_func
            prev_func = current_func
            name = f"{current_class}.{node.name}" if current_class else node.name
            current_func = name
            self.generic_visit(node)
            current_func = prev_func
            
        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_Call(self, node):
            if current_func:
                if isinstance(node.func, ast.Name):
                    calls.add((current_func, node.func.id))
                elif isinstance(node.func, ast.Attribute):
                    calls.add((current_func, node.func.attr))
            self.generic_visit(node)
            
    Visitor().visit(tree)
    return calls


def _ingested_key(repo_name: str, rel_path: str) -> str:
    """ingested_files.file_path key for a (repo, path) pair.

    ingested_files' real primary key is (collection, file_path) with no
    repo column at all -- fine for rules/skills/sessions, each a single
    fixed corpus, but not for code once many independent repos share it:
    two repos' "src/__init__.py" collided under the same bare key, so
    whichever repo ingested second silently inherited the first's hash
    (breaking the incremental skip check) and a --force rebuild's GC step
    (`NOT (file_path = ANY(indexed_paths))`, scoped to repo_path's OWN
    files) deleted every OTHER repo's rows outright, because nothing in
    the query said "and only this repo's rows" (found via T-953 follow-up
    audit: ingested_files WHERE collection='code' held ~57 rows after a
    27-repo --force sweep that should have left ~1000+). Prefixing the
    repo name here -- instead of migrating the shared table, which 3 other
    modules also CREATE TABLE IF NOT EXISTS against -- fixes both without
    touching rules_index.py/skills_index.py/sessions_index.py at all.
    """
    return f"{repo_name}::{rel_path}"


def _code_chunk_and_ingested_counts(cur) -> tuple[dict[str, int], dict[str, int]]:
    """Per-repo (code_chunks rows, ingested_files rows) for the code collection.

    ingested_files has no `repo` column (see `_ingested_key` above); a
    repo's own rows are only identifiable by the `"<repo>::<path>"` prefix
    it writes into `file_path`. Never SQL `LIKE` here -- a repo name
    containing "_" is a wildcard to LIKE, same reasoning as the existing
    --force GC path in `ingest()` below. Returns (chunk_counts,
    file_counts), each {repo_name: count}, used by both `repo_status()`
    (T-970 DoD #5, `--status`) and `_diverged_repo_names()` (the sweep
    self-heal check) so the two never disagree about what "diverged"
    means.
    """
    cur.execute("SELECT repo, count(*) FROM code_chunks GROUP BY repo")
    chunk_counts = dict(cur.fetchall())
    cur.execute("SELECT file_path FROM ingested_files WHERE collection = 'code'")
    file_counts: dict[str, int] = {}
    for (key,) in cur.fetchall():
        repo, sep, _rest = key.partition("::")
        if sep:
            file_counts[repo] = file_counts.get(repo, 0) + 1
    return chunk_counts, file_counts


def repo_status() -> dict:
    """Per-repo status for the code collection: {repo: {chunks, files, diverged}}.

    Used by `rag_ingest.py --status` (T-970 DoD #5) so a diverged repo --
    files registered in `ingested_files`, zero rows in `code_chunks` -- is
    visible in one command instead of the hand-written SQL it took to find
    this state in the first place. Returns {} when POSTGRES_DSN is unset
    OR Postgres is unreachable -- callers must treat that as "unknown",
    never as "no repos are diverged" (fail toward not blocking --status on
    a DB problem, same philosophy as hooks/code_lookup_gate.py's
    _repo_has_chunks).
    """
    load_env()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return {}
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                chunk_counts, file_counts = _code_chunk_and_ingested_counts(cur)
    except psycopg.OperationalError:
        return {}
    result = {}
    for repo in set(chunk_counts) | set(file_counts):
        chunks = chunk_counts.get(repo, 0)
        files = file_counts.get(repo, 0)
        result[repo] = {"chunks": chunks, "files": files, "diverged": files > 0 and chunks == 0}
    return result


def _diverged_repo_names(cur, roots: list[Path]) -> list[str]:
    """Names (each root's own basename) currently diverged: `ingested_files`
    rows > 0 for that repo, `code_chunks` rows == 0 for that repo."""
    chunk_counts, file_counts = _code_chunk_and_ingested_counts(cur)
    return [r.name for r in roots if file_counts.get(r.name, 0) > 0 and chunk_counts.get(r.name, 0) == 0]


def _sweep_lock_path() -> Path:
    """Vault-relative flock target guarding sweep()/cmd_reindex() against
    concurrent runs (see _SweepLock)."""
    return delegate.DATA_DIR / "code_index.lock"


class _SweepLock:
    """Non-blocking, whole-process flock so at most one ingest/sweep touches
    the code index at a time.

    T-970 root cause: three unlocked writers overlapped (a manual --force
    sweep, the launchd `com.ai-router.rag-sweep` timer, and a scoped
    `code_index.py reindex` from a parallel session), and one run's
    committed writes were silently overwritten by another's now-stale view
    of the world -- 10,640 code_chunks rows collapsed to 152 while every
    run still reported `chunks_deleted: 0`. A single `flock` on one file,
    acquired non-blocking around `sweep()`'s whole body and around
    `cmd_reindex()`'s `ingest()` call, makes those three entry points
    mutually exclusive without touching the DB schema or transactions.
    Non-blocking on purpose: launchd fires every 30 minutes, so a busy
    lock should skip this cycle and let the next one retry, never queue
    behind a long cold ingest.
    """

    def __init__(self):
        self._fh = None

    def acquire(self) -> bool:
        path = _sweep_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


@contextmanager
def _sweep_lock():
    """`with _sweep_lock() as locked:` -- locked is False if another
    process already holds it; callers must skip their work in that case,
    never block waiting for it."""
    lock = _SweepLock()
    got = lock.acquire()
    try:
        yield got
    finally:
        if got:
            lock.release()


def _heal_diverged_repo(dsn: str, repo_name: str) -> None:
    """Drop repo_name's stale ingested_files rows so the follow-up
    force=True ingest actually rewrites every file instead of trusting
    content hashes that no longer correspond to any code_chunks row (the
    exact trap this WO fixes: an unchanged file's hash still matches its
    old ingested_files row, so the per-file skip check in ingest() short-
    circuits it forever once code_chunks has been wiped out from under
    it). Scoped to this repo's own `"<repo>::"` prefix only -- fetched and
    filtered in Python, never via SQL LIKE, for the same reason as the
    --force GC path below in ingest(): a repo name containing "_" (e.g.
    research_toolkit) is itself a LIKE wildcard.
    """
    prefix = f"{repo_name}::"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT file_path FROM ingested_files WHERE collection = 'code'")
            stale_keys = [r[0] for r in cur.fetchall() if r[0].startswith(prefix)]
            if stale_keys:
                cur.execute(
                    "DELETE FROM ingested_files WHERE collection = 'code' AND file_path = ANY(%s)",
                    (stale_keys,),
                )
        conn.commit()


def ingest(force: bool = False, repo_path: Path | None = None) -> dict:
    load_env()
    if repo_path is None:
        repo_path = Path.cwd()
        repo_name, commit = project_info()
    else:
        repo_path = Path(repo_path)
        repo_name, commit = _project_info_for(repo_path)
    if not repo_name:
        repo_name = "ai-router"
    validate_repo_name(repo_name)
    if not commit:
        commit = "unknown"

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN not set")

    stats = {"files_seen": 0, "chunks_written": 0, "chunks_deleted": 0, "skipped": 0, "files_failed": 0}
    
    with psycopg.connect(dsn) as conn:
        init_db(conn)
        
        indexed_commit = None
        if not force:
            with conn.cursor() as cur:
                cur.execute("SELECT repo_commit FROM code_chunks WHERE repo = %s LIMIT 1", (repo_name,))
                row = cur.fetchone()
                if row:
                    indexed_commit = row[0]
                    
        target_files = []
        if indexed_commit and indexed_commit != "unknown" and indexed_commit != commit:
            try:
                res = subprocess.run(["git", "diff", "--name-only", f"{indexed_commit}..HEAD"],
                                     cwd=repo_path, capture_output=True, text=True, check=True)
                changed_files = res.stdout.splitlines()
                target_files = [repo_path / f for f in changed_files if (repo_path / f).exists() and _lang_for_path(f) and not _is_excluded(f)]
                # Vanished files
                vanished = [f for f in changed_files if not (repo_path / f).exists()]
                if vanished:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM code_chunks WHERE repo = %s AND path = ANY(%s) RETURNING id", (repo_name, vanished))
                        stats["chunks_deleted"] += len(cur.fetchall())
                        cur.execute("DELETE FROM ingested_files WHERE collection = 'code' AND file_path = ANY(%s)", ([_ingested_key(repo_name, f) for f in vanished],))
            except subprocess.CalledProcessError:
                # fallback to all
                pass
        
        if not target_files and not (indexed_commit and not force):
            # full rebuild / fallback
            target_files = []
            try:
                res = subprocess.run(["git", "ls-files", "--", *CODE_GLOBS], cwd=repo_path, capture_output=True, text=True, check=True)
                target_files = [repo_path / f for f in res.stdout.splitlines() if (repo_path / f).exists() and not _is_excluded(f)]
            except subprocess.CalledProcessError as e:
                # Not a real git repo (or otherwise unreadable) -- log so
                # this is distinguishable from "genuinely 0 code files",
                # then continue with target_files empty; the repo is
                # skipped, not the whole sweep (WO DoD #5).
                print(f"code_index: {repo_path} is not readable as a git repo, skipping: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
                
        if not target_files and not force:
            # nothing changed
            # just update commit hash maybe?
            with conn.cursor() as cur:
                cur.execute("UPDATE code_chunks SET repo_commit = %s WHERE repo = %s", (commit, repo_name))
                cur.execute("SELECT count(*) FROM code_chunks WHERE repo = %s", (repo_name,))
                stats["total_chunks"] = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM ingested_files WHERE collection = 'code'")
                stats["total_docs"] = cur.fetchone()[0]
            conn.commit()
            return stats
            
        chunk_map = _chunk_files_subprocess([f.resolve() for f in target_files])
        model = get_model()

        for filepath in target_files:
            try:
                rel_path = str(filepath.resolve().relative_to(repo_path.resolve()))
            except ValueError:
                rel_path = str(filepath)

            lang = _lang_for_path(rel_path) or "unknown"
            try:
                source = filepath.read_text('utf-8')
                file_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            except Exception as e:
                print(f"code_index: skipping unreadable file {filepath}: {e}", file=sys.stderr)
                stats.setdefault("files_failed", 0)
                stats["files_failed"] += 1
                continue

            if not force:
                with conn.cursor() as cur:
                    cur.execute("SELECT content_hash FROM ingested_files WHERE collection = 'code' AND file_path = %s", (_ingested_key(repo_name, rel_path),))
                    row = cur.fetchone()
                    if row and row[0] == file_hash:
                        stats["skipped"] += 1
                        continue

            stats["files_seen"] += 1

            chunks = chunk_map.get(str(filepath.resolve()))
            if chunks is None:
                continue

            current_shas = []
            
            # print(f"File {filepath}: generated {len(chunks)} chunks", file=sys.stderr)
            # sys.stderr.flush()
            
            chunk_records = []
            for c in chunks:
                chunk_text = c['text']
                chunk_sha = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                current_shas.append(chunk_sha)
                
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM code_chunks WHERE repo = %s AND path = %s AND chunk_hash = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute("UPDATE code_chunks SET repo_commit = %s WHERE id = %s", (commit, row[0]))
                        chunk_records.append((row[0], c['symbol']))
                        continue
                        
                    try:
                        header = f"{lang} {c['symbol'] or 'module'} in {rel_path}"
                        emb = model.embed([chunk_text], prefix=f"{header}\npassage: ")[0].tolist()
                    except Exception as e:
                        print(f"Embedding failed: {e}", file=sys.stderr)
                        sys.stderr.flush()
                        raise
                    
                    cur.execute(
                        "INSERT INTO code_chunks (repo, path, lang, symbol, parent_symbol, start_line, end_line, chunk, chunk_hash, repo_commit, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (repo_name, rel_path, lang, c['symbol'], c['parent_symbol'], c['start_line'], c['end_line'], chunk_text, chunk_sha, commit, str(emb))
                    )
                    chunk_id = cur.fetchone()[0]
                    chunk_records.append((chunk_id, c['symbol']))
                    stats["chunks_written"] += 1

            # GC chunks no longer in this file
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM code_chunks WHERE repo = %s AND path = %s AND NOT (chunk_hash = ANY(%s)) RETURNING id",
                    (repo_name, rel_path, current_shas)
                )
                stats["chunks_deleted"] += len(cur.fetchall())
                
                cur.execute(
                    "INSERT INTO ingested_files (collection, file_path, content_hash, updated_at) "
                    "VALUES ('code', %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (collection, file_path) DO UPDATE SET content_hash = EXCLUDED.content_hash, updated_at = CURRENT_TIMESTAMP",
                    (_ingested_key(repo_name, rel_path), file_hash)
                )

            # Update call graph for Python
            if lang == 'python':
                calls = extract_python_calls(source)
                with conn.cursor() as cur:
                    for cid, sym in chunk_records:
                        cur.execute("DELETE FROM code_edges WHERE caller_id = %s", (cid,))
                        for caller_sym, callee_sym in calls:
                            if caller_sym == sym:
                                cur.execute("INSERT INTO code_edges (caller_id, callee_symbol) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cid, callee_sym))

        # Resolve edges
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE code_edges ce
                SET resolved_id = cc.id
                FROM code_chunks cc
                WHERE ce.callee_symbol = cc.symbol AND cc.repo = %s
            """, (repo_name,))
            # GC vanished paths entirely when rebuild
            if force:
                indexed_paths = [str(f.resolve().relative_to(repo_path.resolve())) for f in target_files]
                cur.execute("DELETE FROM code_chunks WHERE repo = %s AND NOT (path = ANY(%s)) RETURNING id", (repo_name, indexed_paths))
                stats["chunks_deleted"] += len(cur.fetchall())

                # ingested_files has no repo column (see _ingested_key) --
                # "NOT IN indexed_paths" alone would delete every OTHER
                # repo's rows too. Fetch this repo's own keys by prefix in
                # Python (not SQL LIKE: a repo name containing "_", e.g.
                # research_toolkit, is a wildcard to LIKE) and delete only
                # the ones that actually vanished from this repo.
                prefix = f"{repo_name}::"
                cur.execute("SELECT file_path FROM ingested_files WHERE collection = 'code'")
                existing_keys = [r[0] for r in cur.fetchall() if r[0].startswith(prefix)]
                current_keys = {_ingested_key(repo_name, p) for p in indexed_paths}
                stale_keys = [k for k in existing_keys if k not in current_keys]
                if stale_keys:
                    cur.execute("DELETE FROM ingested_files WHERE collection = 'code' AND file_path = ANY(%s)", (stale_keys,))
            
            cur.execute("UPDATE code_chunks SET repo_commit = %s WHERE repo = %s", (commit, repo_name))
            cur.execute("SELECT count(*) FROM code_chunks WHERE repo = %s", (repo_name,))
            stats["total_chunks"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ingested_files WHERE collection = 'code'")
            stats["total_docs"] = cur.fetchone()[0]

        conn.commit()
    return stats


def sweep(force: bool = False, budget_seconds: float | None = None) -> dict:
    """Ingest every configured repo root (get_repo_roots()), one at a time.

    Used by the `rag_ingest.py --collection code` sweep (the launchd job
    com.ai-router.rag-sweep). Must always be safe to call repeatedly and
    must never let one bad repo or a long cold repo starve the rest:
    - Per-repo failure isolation: any exception from ingest() for one repo
      (bad encoding, no git, broken symlink, permissions, etc.) is logged
      to stderr and that repo is skipped; the sweep continues with the
      next repo. The ONE exception is psycopg.OperationalError (Postgres
      itself unreachable) -- that is not a per-repo problem, so it is
      re-raised immediately so the caller (rag_ingest.py) can report the
      real outage instead of it looking like 30+ repo failures.
    - Time budget: stops starting new repos once `budget_seconds` has
      elapsed since the sweep started (default: env var
      RAG_CODE_SWEEP_BUDGET_S if set, else 1500 seconds -- comfortably
      under the 30-minute launchd interval). A repo already in progress
      finishes; the NEXT repo is what gets deferred.
    - Resume: repos are visited in a fixed order, round-robin-rotated by
      an index persisted in <vault>/data/code_sweep_state.json
      ({"next_index": N}) so a run that hits the budget resumes with the
      repos it didn't get to last time, instead of starving the same
      tail of the list forever. Missing/corrupt state file starts at 0.
    - Concurrency: the whole call is wrapped in `_sweep_lock()` (T-970) --
      if another sweep or `code_index.py reindex` already holds it, this
      call does no work at all and returns immediately with
      `locked_out: True` in the stats, so the launchd job still exits 0
      and simply retries on its next 30-minute tick.
    - Divergence self-heal (T-970 DoD 1/2): after the normal per-repo pass,
      every configured root is checked for the failure this WO fixes --
      rows in `ingested_files` but zero rows in `code_chunks` for that
      repo, which the old skip-cache check could never notice on its own.
      Each diverged repo has its stale `ingested_files` rows dropped
      (`_heal_diverged_repo`) and is force-reingested, scoped to that repo
      only -- healing one repo never touches another's rows. Healing
      respects the same `budget_seconds` deadline as the main pass: a
      repo left unhealed when the budget runs out is reported in
      `empty_repos_unhealed`, not silently dropped, and the next sweep
      will find it still diverged and try again. A healed divergence is a
      success (`empty_repos_unhealed == []`); an unhealed one is what
      `rag_ingest.py` treats as loud (DoD 4).
    """
    load_env()
    if budget_seconds is None:
        budget_seconds = float(os.environ.get("RAG_CODE_SWEEP_BUDGET_S", 1500))
    dsn = os.environ.get("POSTGRES_DSN")

    with _sweep_lock() as locked:
        if not locked:
            print("code_index: sweep skipped -- another ingest/sweep holds the lock", file=sys.stderr)
            return {
                "repos_seen": 0, "repos_failed": 0, "files_seen": 0,
                "chunks_written": 0, "chunks_deleted": 0, "skipped": 0,
                "failures": {}, "locked_out": True,
                "empty_repos": [], "empty_repos_healed": [], "empty_repos_unhealed": [],
            }

        import time
        roots = get_repo_roots()
        state_path = delegate.DATA_DIR / "code_sweep_state.json"
        try:
            state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
        except Exception:
            state = {}
        start_idx = (state.get("next_index", 0) % len(roots)) if roots else 0
        order = roots[start_idx:] + roots[:start_idx]

        t0 = time.time()
        total = {
            "repos_seen": 0, "repos_failed": 0, "files_seen": 0,
            "chunks_written": 0, "chunks_deleted": 0, "skipped": 0,
            "failures": {},
        }
        processed = 0
        for root in order:
            if time.time() - t0 > budget_seconds:
                break
            try:
                stats = ingest(force=force, repo_path=root)
                for k in ("files_seen", "chunks_written", "chunks_deleted", "skipped"):
                    total[k] += stats.get(k, 0)
            except psycopg.OperationalError:
                raise
            except Exception as e:
                total["repos_failed"] += 1
                total["failures"][str(root)] = str(e)
                print(f"code_index: skipping repo {root}: {e}", file=sys.stderr)
            finally:
                total["repos_seen"] += 1
                processed += 1

        delegate.DATA_DIR.mkdir(parents=True, exist_ok=True)
        next_index = ((start_idx + processed) % len(roots)) if roots else 0
        state_path.write_text(json.dumps({"next_index": next_index}), "utf-8")

        # --- Divergence detection + self-heal (T-970 DoD 1/2) ---
        total["empty_repos"] = []
        total["empty_repos_healed"] = []
        total["empty_repos_unhealed"] = []
        if dsn:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    diverged = _diverged_repo_names(cur, roots)
            total["empty_repos"] = diverged
            if diverged:
                print(f"code_index: divergence detected in {len(diverged)} repo(s): {diverged}", file=sys.stderr)
            roots_by_name = {r.name: r for r in roots}
            for repo_name in diverged:
                if time.time() - t0 > budget_seconds:
                    total["empty_repos_unhealed"].append(repo_name)
                    continue
                root = roots_by_name.get(repo_name)
                if root is None:
                    total["empty_repos_unhealed"].append(repo_name)
                    continue
                try:
                    _heal_diverged_repo(dsn, repo_name)
                    heal_stats = ingest(force=True, repo_path=root)
                    for k in ("files_seen", "chunks_written", "chunks_deleted", "skipped"):
                        total[k] += heal_stats.get(k, 0)
                    total["empty_repos_healed"].append(repo_name)
                except psycopg.OperationalError:
                    raise
                except Exception as e:
                    total["empty_repos_unhealed"].append(repo_name)
                    total["failures"][f"heal:{repo_name}"] = str(e)
                    print(f"code_index: failed to heal diverged repo {repo_name}: {e}", file=sys.stderr)
            if total["empty_repos_healed"] or total["empty_repos_unhealed"]:
                print(
                    f"code_index: healed {total['empty_repos_healed']}, "
                    f"still diverged {total['empty_repos_unhealed']}",
                    file=sys.stderr,
                )

        if dsn:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM code_chunks")
                    total["total_chunks"] = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM ingested_files WHERE collection = 'code'")
                    total["total_docs"] = cur.fetchone()[0]

        return total


def cmd_reindex(args):
    try:
        force = getattr(args, "rebuild", False) or getattr(args, "force", False)
        with _sweep_lock() as locked:
            if not locked:
                print("code_index: another ingest/sweep holds the lock, skipping", file=sys.stderr)
                return
            ingest(force=force)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_search(args, repo: str | None = None):
    load_env()
    all_repos = getattr(args, "all_repos", False)
    if not all_repos:
        if repo:
            repo_name = repo
            commit = None
        else:
            repo_name, commit = project_info()
            if not repo_name:
                repo_name = "ai-router"
    else:
        repo_name = None
        commit = None
    
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Error: POSTGRES_DSN not set.", file=sys.stderr)
        sys.exit(1)
        
    query = args.query
    k = args.k
    
    model = get_model()
    q_emb = model.embed([query], prefix="query: ")[0].tolist()
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            if not all_repos:
                cur.execute("SELECT repo_commit FROM code_chunks WHERE repo = %s LIMIT 1", (repo_name,))
                row = cur.fetchone()
                if row and commit and row[0] != commit:
                    print(f"Warning: code index is stale. Index commit: {row[0]}, Current commit: {commit}", file=sys.stderr)
                
            if all_repos:
                if args.repo:
                    where_clause = "WHERE path LIKE %s"
                    params = [f"{args.repo}%", str(q_emb), k]
                else:
                    where_clause = ""
                    params = [str(q_emb), k]
            else:
                where_clause = "WHERE repo = %s"
                params = [repo_name]
                if args.repo:
                    where_clause += " AND path LIKE %s"
                    params.append(f"{args.repo}%")
                params.extend([str(q_emb), k])
                
            cur.execute(f"""
                SELECT id, path, start_line, end_line, symbol, chunk, repo 
                FROM code_chunks 
                {where_clause}
                ORDER BY embedding <=> %s::vector 
                LIMIT %s
            """, params)
            
            results = cur.fetchall()
            
            if args.graph and results:
                hit_ids = [r[0] for r in results]
                # Fetch 1-hop callers
                cur.execute("""
                    SELECT cc.id, cc.path, cc.start_line, cc.end_line, cc.symbol, cc.chunk, cc.repo
                    FROM code_edges ce
                    JOIN code_chunks cc ON ce.caller_id = cc.id
                    WHERE ce.resolved_id = ANY(%s)
                """, (hit_ids,))
                callers = cur.fetchall()
                
                # Fetch 1-hop callees
                cur.execute("""
                    SELECT cc.id, cc.path, cc.start_line, cc.end_line, cc.symbol, cc.chunk, cc.repo
                    FROM code_edges ce
                    JOIN code_chunks cc ON ce.resolved_id = cc.id
                    WHERE ce.caller_id = ANY(%s)
                """, (hit_ids,))
                callees = cur.fetchall()
                
                all_res = {r[0]: r for r in results}
                for r in callers + callees:
                    if r[0] not in all_res:
                        all_res[r[0]] = r
                results = list(all_res.values())
            
    out = []
    total_chars = 0
    for r in results:
        path, start_line, end_line, symbol, chunk = r[1], r[2], r[3], r[4], r[5]
        repo_col = r[6] if len(r) > 6 else (repo_name or "")
        s = symbol if symbol else "unknown"
        if all_repos:
            prefix = f"[{repo_col}] {path}:{start_line}-{end_line} [{s}]"
        else:
            prefix = f"{path}:{start_line}-{end_line} [{s}]"
        item = f"{prefix}\n{chunk}\n"
        if total_chars + len(item) > 8000:  # ~2k tokens
            break
        out.append(item)
        total_chars += len(item)
        
    print("\n---\n".join(out))

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    
    p_reindex = subparsers.add_parser("reindex")
    p_reindex.add_argument("--rebuild", action="store_true")
    p_reindex.add_argument("--force", action="store_true")
    
    p_search = subparsers.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=5)
    p_search.add_argument("--graph", action="store_true")
    p_search.add_argument("--repo")
    p_search.add_argument("--all-repos", action="store_true")
    
    p_chunk = subparsers.add_parser("chunk-files")
    p_chunk.add_argument("paths", nargs="*", type=reject_flag_like)

    args = parser.parse_args()
    try:
        if args.cmd == "reindex":
            cmd_reindex(args)
        elif args.cmd == "search":
            cmd_search(args)
        elif args.cmd == "chunk-files":
            cmd_chunk_files(args.paths)
    except psycopg.OperationalError:
        sys.exit("❌ Postgres not reachable — start it first: colima start")

if __name__ == "__main__":
    main()
