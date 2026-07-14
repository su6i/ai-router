#!/usr/bin/env python3
import subprocess
import re
import os

def generate_repo_map(cwd="."):
    try:
        files = subprocess.run(["git", "ls-files"], cwd=cwd, capture_output=True, text=True, check=True).stdout.splitlines()
    except Exception:
        return ""

    out = ["=== REPO MAP ==="]
    
    py_def_re = re.compile(r"^(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
    py_class_re = re.compile(r"^class\s+([a-zA-Z_][a-zA-Z0-9_]*)\b")
    sh_func_re = re.compile(r"^(?:function\s+)?([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\(\)\s*\{")

    for f in files:
        if not f.endswith((".py", ".sh")):
            continue
        path = os.path.join(cwd, f)
        if not os.path.isfile(path):
            continue
        
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            continue
        
        symbols = []
        for line in lines:
            if f.endswith(".py"):
                m = py_def_re.match(line) or py_class_re.match(line)
            else:
                m = sh_func_re.match(line)
            if m:
                symbols.append(m.group(1))
        
        if symbols:
            out.append(f"{f}: {', '.join(symbols)}")
        else:
            out.append(f"{f}")

    out.append("================\n")
    res = "\n".join(out)
    if len(res) > 4000:
        res = res[:3900] + "\n...[TRUNCATED_DUE_TO_SIZE]...\n================"
    return res

if __name__ == "__main__":
    print(generate_repo_map())
