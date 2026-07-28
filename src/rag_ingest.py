import argparse
import sys
import json
import psycopg
from datetime import datetime, timezone
from pathlib import Path

# Add src/ to sys.path so we can import internal modules easily
sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegate as d
import rules_index
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
    
    for collection in ["rules", "sessions", "code"]:
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

def main():
    parser = argparse.ArgumentParser(description="Unified RAG Ingest")
    parser.add_argument("--collection", choices=["rules", "sessions", "code", "all"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_out")
    parser.add_argument("--status", action="store_true")
    
    args = parser.parse_args()
    
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
        parser.error("Either --collection or --status is required")
        
    collections = ["rules", "sessions", "code"] if args.collection == "all" else [args.collection]
    
    import time
    
    modules = {
        "rules": rules_index,
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
