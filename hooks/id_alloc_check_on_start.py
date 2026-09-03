import json
import os
import subprocess
import sys
from pathlib import Path

CHECK_TIMEOUT_S = 5.0

def main():
    try:
        sys.stdin.read()

        src_dir = str(Path(__file__).resolve().parent.parent / "src")

        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir

        res = subprocess.run(
            [sys.executable, "-m", "id_alloc", "check"],
            cwd=src_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S,
        )
        
        out_json = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ""
            }
        }
        
        if res.returncode != 0:
            out_json["hookSpecificOutput"]["additionalContext"] = res.stdout.strip()
            
        print(json.dumps(out_json))
    except Exception:
        out_json = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ""
            }
        }
        print(json.dumps(out_json))
    finally:
        sys.exit(0)

if __name__ == "__main__":
    main()
