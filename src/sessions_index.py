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
    conn.commit()

def cmd_reindex(args):
    load_env()
    
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Error: POSTGRES_DSN not set.", file=sys.stderr)
        sys.exit(1)
        
    agent_projects = _agent_projects_root()
    
    with psycopg.connect(dsn) as conn:
        init_db(conn)
        
        target_files = []
        if agent_projects.exists():
            for pdir in agent_projects.iterdir():
                if pdir.is_dir():
                    sess_file = pdir / "workspace" / "SESSION.md"
                    if sess_file.exists():
                        target_files.append(sess_file)
        
        model = E5Model()
        indexed_repos = set()
        
        for filepath in target_files:
            repo_name = filepath.parent.parent.name
            indexed_repos.add(repo_name)
            
            rel_path = "workspace/SESSION.md"

            text = filepath.read_text()
            chunks = chunk_markdown(text)
            current_shas = []

            for chunk in chunks:
                chunk_text = chunk["text"]
                chunk_sha = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                current_shas.append(chunk_sha)
                
                heading = chunk["heading"]
                m = re.search(r'(\d{4}-\d{2}-\d{2})', heading)
                date_val = m.group(1) if m else None
                
                # Check if exists
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM session_chunks WHERE repo = %s AND path = %s AND chunk_sha = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        continue
                    
                    # Compute embedding
                    emb = model.embed([chunk_text], prefix="passage: ")[0].tolist()
                    
                    cur.execute(
                        "INSERT INTO session_chunks (repo, path, heading, start_line, chunk, chunk_sha, date, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (repo_name, rel_path, heading, chunk["start_line"], chunk_text, chunk_sha, date_val, str(emb))
                    )

            # GC: drop chunks this file no longer contains
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_chunks WHERE repo = %s AND path = %s AND NOT (chunk_sha = ANY(%s))",
                    (repo_name, rel_path, current_shas)
                )

        # GC: drop paths that vanished from the corpus entirely.
        if indexed_repos:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_chunks WHERE NOT (repo = ANY(%s))",
                    (list(indexed_repos),)
                )
        else:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM session_chunks")
        conn.commit()

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
    
    subparsers.add_parser("reindex")
    
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
