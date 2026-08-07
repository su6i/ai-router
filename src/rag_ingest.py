import argparse
import hashlib
import json
import os
import psycopg
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src/ to sys.path so we can import internal modules easily
sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegate as d
import rules_index
import skills_index
import sessions_index
import code_index

def _state_file():
    return d.DATA_DIR / "rag_state.json"

def rag_freshness() -> dict:
    state_path = _state_file()
    
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text("utf-8"))
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}

    now = datetime.now(timezone.utc)
    
    for collection in ["rules", "skills", "sessions", "code"]:
        if collection not in state:
            state[collection] = {
                "last_ingest": None,
                "status": "failed",
                "last_error": "Never ingested",
                "docs": 0,
                "chunks": 0,
                "stale": True
            }
        else:
            col_state = state[collection]
            stale = False
            if col_state.get("status") == "failed":
                stale = True
            elif col_state.get("last_ingest"):
                try:
                    # fromisoformat requires exactly ISO format, might need to handle Z
                    last_str = col_state["last_ingest"].replace("Z", "+00:00")
                    last_dt = datetime.fromisoformat(last_str)
                    if (now - last_dt).total_seconds() > 24 * 3600:
                        stale = True
                except ValueError:
                    stale = True
            else:
                stale = True
            col_state["stale"] = stale
            
    return state

def _update_state(collection: str, stats: dict, error_msg: str = None):
    state_path = _state_file()
    
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text("utf-8"))
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
        
    state.setdefault(collection, {})
    now_iso = datetime.now(timezone.utc).isoformat()
    
    if error_msg:
        state[collection].update({
            "last_ingest": now_iso,
            "status": "failed",
            "last_error": error_msg
        })
    else:
        state[collection].update({
            "last_ingest": now_iso,
            "status": "ok",
            "last_error": None,
            # total_docs/total_chunks are the collection's current row counts
            # (queried fresh each run by the indexer's ingest()), NOT this
            # run's delta — a no-op incremental run still has
            # files_seen=chunks_written=0, and code_index.py's commit-based
            # short-circuit doesn't even walk files when nothing changed, so
            # a delta-based count would wrongly read back as 0/stale-looking
            # for an otherwise healthy collection. Fall back to the delta
            # only if an indexer hasn't been updated to report totals.
            "docs": stats.get("total_docs", stats.get("files_seen", 0) + stats.get("skipped", 0)),
            "chunks": stats.get("total_chunks", stats.get("chunks_written", 0))
        })
        
    d.DATA_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), "utf-8")

def handle_receipt(file_path_str: str, collection_arg: str | None = None):
    d.load_env()
    path = Path(file_path_str).resolve()
    if not path.exists():
        print(f"Error: File not found: {file_path_str}", file=sys.stderr)
        sys.exit(1)
        
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Postgres unavailable — start it first: colima start", file=sys.stderr)
        sys.exit(2)

    try:
        conn = psycopg.connect(dsn)
        conn.close()
    except psycopg.OperationalError:
        print("Postgres unavailable — start it first: colima start", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error connecting to Postgres: {e}", file=sys.stderr)
        sys.exit(2)

    col = collection_arg if collection_arg and collection_arg != "all" else None
    if not col:
        s_path = str(path)
        if "skills" in s_path:
            col = "skills"
        elif "rules" in s_path or path.suffix == ".mdc":
            col = "rules"
        elif path.suffix in (".py", ".js", ".ts", ".go", ".rs"):
            col = "code"
        else:
            col = "sessions"

    if col == "sessions":
        sessions_index.ingest(force=True, target_file=path)
    elif col == "skills":
        skills_index.ingest(force=True, target_file=path)
    elif col == "rules":
        rules_index.ingest(force=True)
    elif col == "code":
        code_index.ingest(force=True)

    agent_projects = d._agent_projects_root()
    try:
        rel_path = path.relative_to(agent_projects).as_posix()
    except ValueError:
        rel_path = str(path)

    table_name = "skill_chunks" if col == "skills" else ("session_chunks" if col == "sessions" else f"{col}_chunks")
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, chunk FROM {table_name} WHERE path = %s ORDER BY id",
                (rel_path,)
            )
            rows = cur.fetchall()

    if not rows:
        print(f"Error: No chunks found in database for {rel_path}", file=sys.stderr)
        sys.exit(1)

    db_text = "".join(r[1] for r in rows)
    db_sha = hashlib.sha256(db_text.encode("utf-8")).hexdigest()[:12]
    chunk_count = len(rows)
    ids = [r[0] for r in rows]
    id_str = f"id:{ids[0]}" if len(ids) == 1 else f"ids:{ids[0]}-{ids[-1]}"

    print(f"RECEIPT col:{col} sha:{db_sha} chunks:{chunk_count} {id_str} path:{rel_path}")

def main():
    parser = argparse.ArgumentParser(description="Unified RAG Ingest")
    parser.add_argument("--collection", choices=["rules", "skills", "sessions", "code", "all"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_out")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--receipt", type=str, help="Ingest single file and print receipt")
    
    args = parser.parse_args()
    
    if getattr(args, "receipt", None):
        handle_receipt(args.receipt, getattr(args, "collection", None))
        return

    if args.status:
        state = rag_freshness()
        if args.json_out:
            print(json.dumps(state, indent=2))
        else:
            for col, data in state.items():
                print(f"{col.upper()}:")
                for k, v in data.items():
                    print(f"  {k}: {v}")
        return

    if not args.collection:
        parser.error("Either --collection, --receipt, or --status is required")
        
    collections = ["rules", "skills", "sessions", "code"] if args.collection == "all" else [args.collection]
    
    import time
    
    modules = {
        "rules": rules_index,
        "skills": skills_index,
        "sessions": sessions_index,
        "code": code_index
    }
    
    any_failed = False
    
    for col in collections:
        mod = modules[col]
        t0 = time.time()
        try:
            stats = mod.ingest(force=args.force)
            duration = time.time() - t0
            
            _update_state(col, stats)
            
            res = {
                "collection": col,
                "files_seen": stats.get("files_seen", 0),
                "chunks_written": stats.get("chunks_written", 0),
                "chunks_deleted": stats.get("chunks_deleted", 0),
                "skipped": stats.get("skipped", 0),
                "duration_s": round(duration, 2),
                "status": "ok"
            }
            if args.json_out:
                print(json.dumps(res))
            else:
                print(f"{col} OK: {stats} in {res['duration_s']}s")
                
        except psycopg.OperationalError:
            print("Postgres unavailable — start it first: colima start", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            duration = time.time() - t0
            _update_state(col, {}, str(e))
            res = {
                "collection": col,
                "status": "failed",
                "error": str(e),
                "duration_s": round(duration, 2)
            }
            if args.json_out:
                print(json.dumps(res))
            else:
                print(f"{col} FAILED: {e}")
            any_failed = True
            
    if any_failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
