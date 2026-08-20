import argparse
import sys
import os
import hashlib
from pathlib import Path
import threading
import time

import psycopg
from tokenizers import Tokenizer
import onnxruntime as ort
import numpy as np

# Set HF_HOME safe fallback BEFORE importing huggingface_hub, as it caches the env var at import time.
if "HF_HOME" not in os.environ:
    rag_hf = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agent-projects" / "_memory" / "rag" / "hf_cache"
    try:
        rag_hf.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    os.environ["HF_HOME"] = str(rag_hf)

from huggingface_hub import hf_hub_download, try_to_load_from_cache

# Import delegate under ONE module identity ("delegate"), whether we run as
# `python -m src.rules_index` (r.sh) or get imported by mcp/server.py, whose
# sys.path already carries src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, project_info  # noqa: E402

_TOKENIZER = None

E5_REPO = "intfloat/multilingual-e5-small"

_MODEL = None
_MODEL_LAST_USED = 0.0
_MODEL_LOCK = threading.Lock()
_MODEL_UNLOAD_THREAD_STARTED = False

RAG_MODEL_IDLE_TTL = int(os.environ.get("RAG_MODEL_IDLE_TTL", "900"))

def _idle_unloader() -> None:
    global _MODEL
    if RAG_MODEL_IDLE_TTL <= 0:
        return  # disabled, e.g. for long reindex runs
    # Check at most every 60s in production (TTL defaults to 900s, no need to
    # poll faster), but never coarser than the TTL itself -- otherwise a short
    # TTL (e.g. RAG_MODEL_IDLE_TTL=3 in scripts/bench_rag_memory.py) would
    # never be observed inside one 60s sleep.
    check_interval = min(60, RAG_MODEL_IDLE_TTL)
    while True:
        time.sleep(check_interval)
        with _MODEL_LOCK:
            if _MODEL is not None and (time.monotonic() - _MODEL_LAST_USED) > RAG_MODEL_IDLE_TTL:
                _MODEL = None

def get_model() -> "E5Model":
    """Process-wide singleton. Building an InferenceSession per call ratchets
    RSS by ~200MB and never returns it to the OS (T-132)."""
    global _MODEL, _MODEL_LAST_USED, _MODEL_UNLOAD_THREAD_STARTED
    with _MODEL_LOCK:
        if _MODEL is None:
            _MODEL = E5Model()
        _MODEL_LAST_USED = time.monotonic()
        if not _MODEL_UNLOAD_THREAD_STARTED:
            _MODEL_UNLOAD_THREAD_STARTED = True
            t = threading.Thread(target=_idle_unloader, daemon=True)
            t.start()
    return _MODEL


def _hf_token_kwargs() -> dict:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    return {"token": token} if token else {}


def _hf_file(filename: str) -> str:
    """Resolve a model file, preferring the local cache over a Hub round-trip.

    Two reasons. The obvious one: these files never change, so contacting the
    Hub on every single query is pure latency. The other: once `tokenizers` is
    imported into the process, any Hub request prints

        Warning: You are sending unauthenticated requests to the HF Hub…

    from a Rust extension straight to stderr — not through Python logging or
    `warnings`, so it cannot be filtered on the Python side, and it is
    unactionable anyway for two small public files. Resolving from cache skips
    the request entirely, so the line appears at most once, on the very first
    download. Setting HF_TOKEN keeps the fast path and authenticates for real.
    """
    try:
        cached = try_to_load_from_cache(repo_id=E5_REPO, filename=filename)
        if isinstance(cached, str) and Path(cached).is_file():
            return cached
        return hf_hub_download(repo_id=E5_REPO, filename=filename, **_hf_token_kwargs())
    except (OSError, PermissionError) as e:
        path = getattr(e, "filename", None) or os.environ.get("HF_HOME", "hf-cache")
        raise RuntimeError(f"RAG unavailable: {path} (index/model not reachable)") from None


def _load_tokenizer() -> Tokenizer:
    """Load the tokenizer from tokenizer.json instead of from_pretrained.

    from_pretrained runs its own downloader, which cannot use the cache-first
    path above. Token ids were verified identical between the two.
    """
    return Tokenizer.from_file(_hf_file("tokenizer.json"))


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = _load_tokenizer()
    return _TOKENIZER

def mean_pooling(last_hidden_states, attention_mask):
    input_mask_expanded = np.expand_dims(attention_mask, -1)
    sum_embeddings = np.sum(last_hidden_states * input_mask_expanded, axis=1)
    sum_mask = np.clip(np.sum(input_mask_expanded, axis=1), a_min=1e-9, a_max=None)
    return sum_embeddings / sum_mask

class E5Model:
    def __init__(self):
        # We only download once (the only allowed network access). huggingface_hub caches it.
        self.model_path = _hf_file("onnx/model.onnx")
        self.tokenizer = _load_tokenizer()
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>"), pad_token="<pad>")
        so = ort.SessionOptions()
        so.enable_cpu_mem_arena = False
        self.session = ort.InferenceSession(self.model_path, so, providers=['CPUExecutionProvider'])
        
    def embed(self, texts, prefix="passage: "):
        global _MODEL_LAST_USED
        _MODEL_LAST_USED = time.monotonic()
        formatted_texts = [prefix + t for t in texts]
        encoded = self.tokenizer.encode_batch(formatted_texts)
        
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        
        inputs = {'input_ids': input_ids, 'attention_mask': attention_mask}
        # e5-small might need token_type_ids if present in model
        input_names = [i.name for i in self.session.get_inputs()]
        if 'token_type_ids' in input_names:
            inputs['token_type_ids'] = np.zeros_like(input_ids)
            
        outputs = self.session.run(None, inputs)
        last_hidden_states = outputs[0]
        
        embeddings = mean_pooling(last_hidden_states, attention_mask)
        # L2 normalization for cosine similarity
        norm = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.clip(norm, a_min=1e-9, a_max=None)
        return embeddings

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rules_chunks (
                id bigserial PRIMARY KEY,
                repo text,
                path text,
                heading text,
                start_line int,
                chunk text,
                chunk_sha text,
                repo_commit text,
                embedding vector(384)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS rules_chunks_embedding_idx 
            ON rules_chunks USING hnsw (embedding vector_cosine_ops);
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

def chunk_markdown(text, max_tokens=300):
    tokenizer = _get_tokenizer()

    lines = text.splitlines()
    chunks = []
    
    current_heading = ""
    current_chunk_lines = []
    current_start_line = 1
    
    def emit_chunk(current_line_idx):
        nonlocal current_chunk_lines, current_start_line
        if not current_chunk_lines:
            return
        
        chunk_text = "\n".join(current_chunk_lines).strip()
        # A chunk that is ONLY its heading (e.g. a title directly followed by
        # the next heading) or has no words at all (e.g. a lone `---` rule)
        # has no retrieval value and pollutes top-k.
        if chunk_text and chunk_text != current_heading \
                and any(c.isalnum() for c in chunk_text):
            chunks.append({
                "heading": current_heading,
                "start_line": current_start_line,
                "text": chunk_text
            })
        current_chunk_lines = []
        current_start_line = current_line_idx + 1

    in_fence = False
    for i, line in enumerate(lines):
        # `#` inside a ``` code fence is a comment, not a heading — without
        # this, bash comments in README examples become bogus chunks.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        # check if heading
        if not in_fence and line.startswith("#") and " " in line:
            parts = line.split(" ", 1)
            if all(c == "#" for c in parts[0]):
                emit_chunk(i)
                current_heading = line.strip()
                current_chunk_lines.append(line)
                current_start_line = i + 1
                continue
                
        # if paragraph break and chunk is big
        if not line.strip():
            # count tokens
            temp_text = "\n".join(current_chunk_lines)
            tokens = len(tokenizer.encode(temp_text).ids)
            if tokens > max_tokens:
                emit_chunk(i)
                continue
                
        current_chunk_lines.append(line)
        
    emit_chunk(len(lines))
    return chunks

def ingest(force: bool = False) -> dict:
    load_env()
    repo_name, commit = project_info()
    
    if not repo_name:
        repo_name = "ai-router"
        
    if not commit:
        # fallback for commit if not in git repo?
        commit = "unknown"

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN not set")
        
    stats = {"files_seen": 0, "chunks_written": 0, "chunks_deleted": 0, "skipped": 0}

    with psycopg.connect(dsn) as conn:
        init_db(conn)
        
        # files: .agent/constitution/rules/*.md, docs/**/*.md, CLAUDE.md
        repo_path = Path.cwd()
        target_files = []
        
        # Follow symlinks with resolve() or explicitly checking .agent/constitution
        constitution_dir = repo_path / ".agent" / "constitution"
        if constitution_dir.exists():
            rules_dir = constitution_dir / "rules"
            if rules_dir.exists():
                for md in rules_dir.glob("*.md"):
                    target_files.append(md)
        
        docs_dir = repo_path / "docs"
        if docs_dir.exists():
            for md in docs_dir.rglob("*.md"):
                # Translations (docs/fa, *.fa.md) duplicate the canonical
                # English content; indexing both drowns cross-lingual queries
                # (a Persian query then only ever hits the Persian mirror,
                # never the canonical rule text). e5 is multilingual: Persian
                # queries still match English chunks.
                if "fa" in md.relative_to(docs_dir).parts or md.name.endswith(".fa.md") or "legacy" in md.relative_to(docs_dir).parts:
                    continue
                target_files.append(md)
                
        claude_md = repo_path / "CLAUDE.md"
        if claude_md.exists():
            target_files.append(claude_md)
            
        model = get_model()

        indexed_paths = []
        for filepath in target_files:
            try:
                rel_path = str(filepath.resolve().relative_to(repo_path.resolve()))
            except ValueError:
                rel_path = str(filepath)
            indexed_paths.append(rel_path)

            text = filepath.read_text()
            file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

            if not force:
                with conn.cursor() as cur:
                    cur.execute("SELECT content_hash FROM ingested_files WHERE collection = 'rules' AND file_path = %s", (rel_path,))
                    row = cur.fetchone()
                    if row and row[0] == file_hash:
                        stats["skipped"] += 1
                        continue
                        
            stats["files_seen"] += 1
            chunks = chunk_markdown(text)
            current_shas = []

            for chunk in chunks:
                chunk_text = chunk["text"]
                chunk_sha = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                current_shas.append(chunk_sha)
                
                # Check if exists
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, repo_commit FROM rules_chunks WHERE repo = %s AND path = %s AND chunk_sha = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        if row[1] != commit:
                            cur.execute(
                                "UPDATE rules_chunks SET repo_commit = %s WHERE id = %s",
                                (commit, row[0])
                            )
                        continue
                    
                    # Compute embedding
                    emb = model.embed([chunk_text], prefix="passage: ")[0].tolist()
                    
                    cur.execute(
                        "INSERT INTO rules_chunks (repo, path, heading, start_line, chunk, chunk_sha, repo_commit, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (repo_name, rel_path, chunk["heading"], chunk["start_line"], chunk_text, chunk_sha, commit, str(emb))
                    )
                    stats["chunks_written"] += 1

            # GC: drop chunks this file no longer contains (edited/deleted
            # paragraphs would otherwise stay and pollute retrieval forever).
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rules_chunks WHERE repo = %s AND path = %s AND NOT (chunk_sha = ANY(%s)) RETURNING id",
                    (repo_name, rel_path, current_shas)
                )
                stats["chunks_deleted"] += len(cur.fetchall())
                
                cur.execute(
                    "INSERT INTO ingested_files (collection, file_path, content_hash, updated_at) "
                    "VALUES ('rules', %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (collection, file_path) DO UPDATE SET content_hash = EXCLUDED.content_hash, updated_at = CURRENT_TIMESTAMP",
                    (rel_path, file_hash)
                )

        # GC: drop paths that vanished from the corpus entirely.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rules_chunks WHERE repo = %s AND NOT (path = ANY(%s)) RETURNING id",
                (repo_name, indexed_paths)
            )
            stats["chunks_deleted"] += len(cur.fetchall())
            
            cur.execute(
                "DELETE FROM ingested_files WHERE collection = 'rules' AND NOT (file_path = ANY(%s))",
                (indexed_paths,)
            )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM rules_chunks WHERE repo = %s", (repo_name,))
            stats["total_chunks"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ingested_files WHERE collection = 'rules'")
            stats["total_docs"] = cur.fetchone()[0]

        conn.commit()
    return stats

def cmd_reindex(args):
    try:
        force = getattr(args, "rebuild", False) or getattr(args, "force", False)
        ingest(force=force)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_search(args):
    load_env()
    repo = getattr(args, "repo", None)
    if repo:
        repo_name = repo
        commit = None
    else:
        repo_name, commit = project_info()
        if not repo_name:
            repo_name = "ai-router"
        
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
            # Check staleness
            cur.execute("SELECT repo_commit FROM rules_chunks WHERE repo = %s LIMIT 1", (repo_name,))
            row = cur.fetchone()
            if row and commit and row[0] != commit:
                print(f"Warning: rules index is stale. Index commit: {row[0]}, Current commit: {commit}", file=sys.stderr)
            
            # HNSW cosine distance (<=>)
            cur.execute("""
                SELECT path, start_line, heading, chunk 
                FROM rules_chunks 
                WHERE repo = %s
                ORDER BY embedding <=> %s::vector 
                LIMIT %s
            """, (repo_name, str(q_emb), k))
            
            results = cur.fetchall()
            
    # output cap enforcement ~8000 chars
    out = []
    total_chars = 0
    for path, start_line, heading, chunk in results:
        h = heading if heading else "No heading"
        prefix = f"{path}:{start_line} [{h}]"
        # ensure prefix is clear
        item = f"{prefix}\n{chunk}\n"
        if total_chars + len(item) > 8000:
            break
        out.append(item)
        total_chars += len(item)
        
    print("\n---\n".join(out))

def main():
    parser = argparse.ArgumentParser(description="Rules index and search")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    
    parser_reindex = subparsers.add_parser("reindex")
    parser_reindex.add_argument("--rebuild", action="store_true", help="Force rebuild")
    parser_reindex.add_argument("--force", action="store_true", help="Force rebuild")
    
    parser_search = subparsers.add_parser("search")
    parser_search.add_argument("query", help="Search query")
    parser_search.add_argument("-k", type=int, default=5, help="Number of results")
    
    args = parser.parse_args()
    try:
        if args.cmd == "reindex":
            cmd_reindex(args)
        elif args.cmd == "search":
            cmd_search(args)
    except psycopg.OperationalError:
        sys.exit("❌ Postgres not reachable — start it first: colima start")

if __name__ == "__main__":
    main()
