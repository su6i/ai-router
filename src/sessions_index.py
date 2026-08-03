import argparse
import sys
import os
import hashlib
from pathlib import Path
import psycopg
import re

# Import delegate under ONE module identity ("delegate")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, _agent_projects_root  # noqa: E402
from rules_index import E5Model, chunk_markdown  # noqa: E402

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS session_chunks (
                id bigserial PRIMARY KEY,
                repo text,
                path text,
                heading text,
                start_line int,
                chunk text,
                chunk_sha text,
                repo_commit text,
                date text,
                embedding vector(384)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS session_chunks_embedding_idx 
            ON session_chunks USING hnsw (embedding vector_cosine_ops);
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

def find_session_files(agent_projects: Path) -> list[Path]:
    target_files = []
    if not agent_projects.exists():
        return target_files

    for pdir in agent_projects.iterdir():
        if not pdir.is_dir():
            continue
        if pdir.name == "_memory":
            sess_dir = pdir / "sessions"
            if sess_dir.exists():
                for f in sess_dir.rglob("*.md"):
                    if f.is_file() and not f.name.startswith("."):
                        target_files.append(f)
            handoff_dir = pdir / "handoffs"
            if handoff_dir.exists():
                for f in handoff_dir.iterdir():
                    if f.is_file() and f.suffix in (".md", ".txt") and not f.name.startswith("."):
                        target_files.append(f)
        else:
            ws = pdir / "workspace"
            if ws.exists():
                sess_file = ws / "SESSION.md"
                if sess_file.exists():
                    target_files.append(sess_file)
                for f in ws.rglob("*"):
                    if f.is_file() and f != sess_file and f.suffix in (".md", ".txt") and not f.name.startswith("."):
                        rel_ws = f.relative_to(ws)
                        if any(part.startswith("archive") for part in rel_ws.parts):
                            target_files.append(f)
    return sorted(target_files)

def _extract_date(heading: str, text: str, filename: str) -> str | None:
    if heading:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', heading)
        if m:
            return m.group(1)
        m = re.search(r'(\d{4}-\d{2})', heading)
        if m:
            return m.group(1)
    m = re.search(r'(?:date|Date):\s*["\']?(\d{4}-\d{2}-\d{2})["\']?', text)
    if m:
        return m.group(1)
    m = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if m:
        return m.group(1)
    m = re.search(r'(\d{4}\d{2}\d{2})', filename)
    if m:
        dstr = m.group(1)
        return f"{dstr[:4]}-{dstr[4:6]}-{dstr[6:8]}"
    m = re.search(r'(\d{4}-\d{2})', filename)
    if m:
        return m.group(1)
    return None

def ingest(force: bool = False, target_file: Path | None = None) -> dict:
    load_env()
    
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN not set")
        
    stats = {"files_seen": 0, "chunks_written": 0, "chunks_deleted": 0, "skipped": 0}
        
    agent_projects = _agent_projects_root()
    
    if target_file:
        target_files = [target_file] if target_file.exists() else []
        is_single_file_mode = True
    else:
        target_files = find_session_files(agent_projects)
        is_single_file_mode = False
    
    with psycopg.connect(dsn) as conn:
        init_db(conn)
        
        model = None
        all_indexed_paths = []
        
        for filepath in target_files:
            try:
                rel_path = filepath.relative_to(agent_projects).as_posix()
                repo_name = filepath.relative_to(agent_projects).parts[0]
            except ValueError:
                rel_path = str(filepath)
                repo_name = filepath.parent.name
            
            all_indexed_paths.append(rel_path)

            text = filepath.read_text("utf-8", errors="ignore")
            file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

            if not force:
                with conn.cursor() as cur:
                    cur.execute("SELECT content_hash FROM ingested_files WHERE collection = 'sessions' AND file_path = %s", (rel_path,))
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
                
                heading = chunk["heading"]
                date_val = _extract_date(heading, text, filepath.name)
                
                # Check if exists
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM session_chunks WHERE repo = %s AND path = %s AND chunk_sha = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        continue
                    
                    if model is None:
                        model = E5Model()
                    
                    # Compute embedding
                    emb = model.embed([chunk_text], prefix="passage: ")[0].tolist()
                    
                    cur.execute(
                        "INSERT INTO session_chunks (repo, path, heading, start_line, chunk, chunk_sha, date, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (repo_name, rel_path, heading, chunk["start_line"], chunk_text, chunk_sha, date_val, str(emb))
                    )
                    stats["chunks_written"] += 1

            # GC: drop chunks this file no longer contains
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_chunks WHERE path = %s AND NOT (chunk_sha = ANY(%s)) RETURNING id",
                    (rel_path, current_shas)
                )
                stats["chunks_deleted"] += len(cur.fetchall())
                
                cur.execute(
                    "INSERT INTO ingested_files (collection, file_path, content_hash, updated_at) "
                    "VALUES ('sessions', %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (collection, file_path) DO UPDATE SET content_hash = EXCLUDED.content_hash, updated_at = CURRENT_TIMESTAMP",
                    (rel_path, file_hash)
                )

        # GC: drop paths that vanished from the corpus entirely (only in full ingest mode)
        if not is_single_file_mode:
            if all_indexed_paths:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM session_chunks WHERE NOT (path = ANY(%s)) RETURNING id",
                        (all_indexed_paths,)
                    )
                    stats["chunks_deleted"] += len(cur.fetchall())
                    
                    cur.execute(
                        "DELETE FROM ingested_files WHERE collection = 'sessions' AND NOT (file_path = ANY(%s))",
                        (all_indexed_paths,)
                    )
            else:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM session_chunks RETURNING id")
                    stats["chunks_deleted"] += len(cur.fetchall())
                    cur.execute("DELETE FROM ingested_files WHERE collection = 'sessions'")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_chunks")
            stats["total_chunks"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ingested_files WHERE collection = 'sessions'")
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
    
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Error: POSTGRES_DSN not set.", file=sys.stderr)
        sys.exit(1)
        
    query = args.query
    k = args.k
    
    model = E5Model()
    q_emb = model.embed([query], prefix="query: ")[0].tolist()
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # HNSW cosine distance (<=>)
            cur.execute("""
                SELECT repo, path, start_line, heading, chunk, date 
                FROM session_chunks 
                ORDER BY embedding <=> %s::vector 
                LIMIT %s
            """, (str(q_emb), k))
            
            results = cur.fetchall()
            
    # output cap enforcement ~8000 chars
    out = []
    total_chars = 0
    for repo, path, start_line, heading, chunk, date in results:
        h = heading if heading else "No heading"
        d = f" [{date}]" if date else ""
        prefix = f"{repo}/{path}:{start_line} [{h}]{d}"
        item = f"{prefix}\n{chunk}\n"
        if total_chars + len(item) > 8000:
            break
        out.append(item)
        total_chars += len(item)
        
    print("\n---\n".join(out))

def main():
    parser = argparse.ArgumentParser(description="Sessions index and search")
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
