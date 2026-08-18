#!/usr/bin/env python3
import os
import re
import sys
import time
import subprocess
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("RAG_MODEL_IDLE_TTL", "3")

from src.rules_index import get_model

# /usr/bin/footprint prints a line like:
#     phys_footprint: 7408 KB
# under "Auxiliary data:" -- value and unit are separate whitespace-split
# tokens, not glued together, and there is no "peak" qualifier on this line
# (that is phys_footprint_peak, a different key we must not match).
_FOOTPRINT_RE = re.compile(r"^\s*phys_footprint:\s*([\d.]+)\s*([KMG]?B)\s*$")

def get_footprint_mb(pid: int) -> float:
    res = subprocess.run(["/usr/bin/footprint", "-p", str(pid)], capture_output=True, text=True, check=True)
    for line in res.stdout.splitlines():
        m = _FOOTPRINT_RE.match(line)
        if m:
            num = float(m.group(1))
            unit = m.group(2)
            if unit == "GB":
                return num * 1024
            if unit == "KB":
                return num / 1024
            return num  # MB or bare B (footprint always uses KB/MB/GB in practice)
    raise RuntimeError(f"could not find phys_footprint in footprint output for pid {pid}:\n{res.stdout}")

def main():
    pid = os.getpid()
    before = get_footprint_mb(pid)

    # Fetch the singleton fresh on every call instead of holding a local
    # `model` variable across the sleep below: a stray reference here would
    # keep the E5Model (and its InferenceSession) alive even after
    # rules_index sets its own module-level _MODEL to None, so the idle
    # unloader's effect would never show up in the third reading.
    for _ in range(30):
        get_model().embed(["test query " * 10], prefix="query: ")

    after = get_footprint_mb(pid)

    time.sleep(10)  # > RAG_MODEL_IDLE_TTL=3 set above, so the idle-unload thread fires

    after_unload = get_footprint_mb(pid)
    
    print(f"before={before} MB")
    print(f"after={after} MB")
    print(f"after_unload={after_unload} MB")
    
    if (after - before) > 300:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
