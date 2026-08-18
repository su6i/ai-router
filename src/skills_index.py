import argparse
import hashlib
import os
from pathlib import Path
import sys

# Set HF_HOME safe fallback BEFORE importing huggingface_hub, as it caches the env var at import time.
if "HF_HOME" not in os.environ:
    rag_hf = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agent-projects" / "_memory" / "rag" / "hf_cache"
    try:
        rag_hf.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    os.environ["HF_HOME"] = str(rag_hf)

import psycopg
from tokenizers import Tokenizer

# Import delegate under ONE module identity ("delegate"), whether we run as
# `python -m src.skills_index` or get imported by mcp/server.py, whose
# sys.path already carries src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, project_info  # noqa: E402
from rules_index import get_model, _hf_file  # noqa: E402

_TOKENIZER = None


def _load_tokenizer() -> Tokenizer:
    """Load the tokenizer from tokenizer.json instead of from_pretrained."""
    return Tokenizer.from_file(_hf_file("tokenizer.json"))


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = _load_tokenizer()
    return _TOKENIZER


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS skill_chunks (
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
            CREATE INDEX IF NOT EXISTS skill_chunks_embedding_idx 
            ON skill_chunks USING hnsw (embedding vector_cosine_ops);
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
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("#") and " " in line:
            parts = line.split(" ", 1)
            if all(c == "#" for c in parts[0]):
                emit_chunk(i)
                current_heading = line.strip()
                current_chunk_lines.append(line)
                current_start_line = i + 1
                continue

        if not line.strip():
            temp_text = "\n".join(current_chunk_lines)
            tokens = len(tokenizer.encode(temp_text).ids)
            if tokens > max_tokens:
                emit_chunk(i)
                continue

        current_chunk_lines.append(line)

    emit_chunk(len(lines))
    return chunks


def ingest(force: bool = False, target_file: Path | None = None,
           root: Path | None = None, prune: bool = True) -> dict:
    load_env()
    repo_name, commit = project_info()

    if not repo_name:
        repo_name = "ai-router"

    if not commit:
        commit = "unknown"

    stats = {"files_seen": 0, "chunks_written": 0, "chunks_deleted": 0, "skipped": 0, "total_chunks": 0, "total_docs": 0}

    # `root` is an explicit injection point so callers (and tests) can name the
    # tree to index instead of depending on process-wide CWD, which any other
    # caller in the same process can change underneath us.
    repo_path = root if root is not None else Path.cwd()
    skills_dir = repo_path / ".agent" / "constitution" / "skills"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return stats

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN not set")

    target_files = []
    if target_file is not None:
        if target_file.exists() and target_file.is_file() and target_file.suffix == ".md":
            target_files.append(target_file)
    else:
        for md in sorted(skills_dir.glob("*.md")):
            if md.is_file():
                target_files.append(md)

    with psycopg.connect(dsn) as conn:
        init_db(conn)

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
                    cur.execute(
                        "SELECT content_hash FROM ingested_files WHERE collection = 'skills' AND file_path = %s",
                        (rel_path,)
                    )
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

                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, repo_commit FROM skill_chunks WHERE repo = %s AND path = %s AND chunk_sha = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        if row[1] != commit:
                            cur.execute(
                                "UPDATE skill_chunks SET repo_commit = %s WHERE id = %s",
                                (commit, row[0])
                            )
                        continue

                    emb = model.embed([chunk_text], prefix="passage: ")[0].tolist()

                    cur.execute(
                        "INSERT INTO skill_chunks (repo, path, heading, start_line, chunk, chunk_sha, repo_commit, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (repo_name, rel_path, chunk["heading"], chunk["start_line"], chunk_text, chunk_sha, commit, str(emb))
                    )
                    stats["chunks_written"] += 1

            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM skill_chunks WHERE repo = %s AND path = %s AND NOT (chunk_sha = ANY(%s)) RETURNING id",
                    (repo_name, rel_path, current_shas)
                )
                stats["chunks_deleted"] += len(cur.fetchall())

                cur.execute(
                    "INSERT INTO ingested_files (collection, file_path, content_hash, updated_at) "
                    "VALUES ('skills', %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (collection, file_path) DO UPDATE SET content_hash = EXCLUDED.content_hash, updated_at = CURRENT_TIMESTAMP",
                    (rel_path, file_hash)
                )

        # Pruning removes every row that the CURRENT file list did not cover, so
        # it is only correct for a full-tree ingest of the canonical skills dir.
        # For a single-file ingest, or any caller indexing a different tree, it
        # would delete the rest of the index as a side effect.
        if prune and target_file is None:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM skill_chunks WHERE repo = %s AND NOT (path = ANY(%s)) RETURNING id",
                    (repo_name, indexed_paths)
                )
                stats["chunks_deleted"] += len(cur.fetchall())

                cur.execute(
                    "DELETE FROM ingested_files WHERE collection = 'skills' AND NOT (file_path = ANY(%s))",
                    (indexed_paths,)
                )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_chunks WHERE repo = %s", (repo_name,))
            stats["total_chunks"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ingested_files WHERE collection = 'skills'")
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
            cur.execute("SELECT repo_commit FROM skill_chunks WHERE repo = %s LIMIT 1", (repo_name,))
            row = cur.fetchone()
            if row and commit and row[0] != commit:
                print(f"Warning: skills index is stale. Index commit: {row[0]}, Current commit: {commit}", file=sys.stderr)

            cur.execute("""
                SELECT path, start_line, heading, chunk 
                FROM skill_chunks 
                WHERE repo = %s
                ORDER BY embedding <=> %s::vector 
                LIMIT %s
            """, (repo_name, str(q_emb), k))

            results = cur.fetchall()

    out = []
    total_chars = 0
    for path, start_line, heading, chunk in results:
        h = heading if heading else "No heading"
        prefix = f"{path}:{start_line} [{h}]"
        item = f"{prefix}\n{chunk}\n"
        if total_chars + len(item) > 8000:
            break
        out.append(item)
        total_chars += len(item)

    print("\n---\n".join(out))


def main():
    parser = argparse.ArgumentParser(description="Skills index and search")
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
