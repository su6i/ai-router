import argparse
import sys
from pathlib import Path

def main():
    try:
        parser = argparse.ArgumentParser(description="Dispatch RAG ingestion")
        parser.add_argument("--on", choices=["constitution-merge", "session-append", "inbox-note"], required=True)
        args, unknown = parser.parse_known_args()
        
        collection_map = {
            "constitution-merge": "rules",
            "session-append": "sessions",
            "inbox-note": "sessions"
        }
        
        if args.on not in collection_map:
            sys.exit(0)
            
        collection = collection_map[args.on]
        
        src_path = Path(__file__).resolve().parent.parent / "src"
        if str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))
            
        import rag_ingest
        
        sys.argv = ["rag_ingest.py", "--collection", collection]
        
        rag_ingest.main()
    except BaseException as e:
        # Log the error but NEVER fail the caller
        if not isinstance(e, SystemExit) or e.code != 0:
            print(f"rag_ingest_hook error: {repr(e)}", file=sys.stderr)
        
    # Explicitly exit 0
    sys.exit(0)

if __name__ == "__main__":
    main()
