import os
import subprocess
import sys
from pathlib import Path

SEED_TIMEOUT_S = 5.0

def main():
    try:
        sys.stdin.read()

        src_dir = str(Path(__file__).resolve().parent.parent / "src")

        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir

        subprocess.run(
            [sys.executable, "-m", "id_alloc", "seed"],
            cwd=src_dir,
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SEED_TIMEOUT_S,
        )
    except Exception:
        pass
    finally:
        sys.exit(0)

if __name__ == "__main__":
    main()
