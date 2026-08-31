import argparse
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

def _repo_root() -> Path:
    """The MAIN checkout, not whatever worktree this file happens to sit in.

    `--fix` writes this path into the user's global ~/.claude.json. Derived from
    `__file__` alone, a run from a throwaway git worktree would repoint the
    global registration at a temp directory that is deleted minutes later — the
    exact class of breakage doctor exists to catch. `--git-common-dir` resolves
    to the main checkout's `.git` from every linked worktree, so its parent is
    the durable repo root. Falls back to the file's own location outside git.
    """
    here = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if out:
            return Path(out).resolve().parent
    except Exception:
        pass
    return here


REPO_ROOT = _repo_root()

def _get_home():
    env_home = os.environ.get("AI_ROUTER_DOCTOR_HOME")
    return Path(env_home) if env_home else Path.home()

def _get_claude_json_path():
    return _get_home() / ".claude.json"

def _get_settings_json_path():
    return _get_home() / ".claude" / "settings.json"

def get_server_tools():
    server_path = REPO_ROOT / "mcp" / "server.py"
    try:
        content = server_path.read_text(encoding="utf-8")
    except Exception:
        return set()
    tools = set()
    for match in re.finditer(r'^def handle_(\w+)\(args: dict\)', content, re.MULTILINE):
        name = match.group(1)
        if name not in {"tools_call", "request"}:
            tools.add(name)
    return tools

def check_mcp_registration():
    p = _get_claude_json_path()
    if not p.exists():
        print("FAIL  mcp-registration  File missing")
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"FAIL  mcp-registration  Unparseable JSON: {e}")
        return False
    
    mcp_servers = data.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        print("FAIL  mcp-registration  mcpServers is not a JSON object")
        return False
        
    ai_router = mcp_servers.get("ai-router")
    if not ai_router:
        print("FAIL  mcp-registration  MISSING — not registered")
        return False
        
    expected_path = str(REPO_ROOT / "mcp" / "server.py")
    actual_type = ai_router.get("type")
    actual_args = ai_router.get("args", [])
    
    if actual_type != "stdio" or not actual_args or actual_args[0] != expected_path:
        actual_path = actual_args[0] if actual_args else "none"
        print(f"FAIL  mcp-registration  STALE-PATH — registered but points at {actual_path}")
        return False
        
    print("OK  mcp-registration  MCP server is correctly registered")
    return True

def fix_mcp_registration():
    p = _get_claude_json_path()
    expected_path = str(REPO_ROOT / "mcp" / "server.py")
    manual_repair = f"claude mcp add --scope user ai-router python3 {expected_path}"
    
    if not p.exists():
        print("Cannot fix: file missing. Run:")
        print(manual_repair)
        sys.exit(1)
        
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        print("Cannot fix: unparseable JSON. Run:")
        print(manual_repair)
        sys.exit(1)
        
    mcp_servers = data.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        print("Cannot fix: mcpServers is not a JSON object. Run:")
        print(manual_repair)
        sys.exit(1)
        
    ai_router = mcp_servers.get("ai-router")
    expected_entry = {
        "type": "stdio",
        "command": "python3",
        "args": [expected_path],
        "env": {}
    }
    
    if ai_router == expected_entry:
        print("already OK")
        return
        
    bak = p.with_name(f".claude.json.bak.{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(p, bak)
    
    mcp_servers["ai-router"] = expected_entry
    new_content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    
    fd, tmp_path = tempfile.mkstemp(dir=p.parent, prefix=".claude.json.tmp.")
    try:
        with os.fdopen(fd, 'w', encoding="utf-8") as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except Exception as e:
        os.unlink(tmp_path)
        raise e

def check_mcp_handshake():
    server_path = str(REPO_ROOT / "mcp" / "server.py")
    proc = subprocess.Popen(
        [sys.executable, server_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    try:
        proc.stdin.write('{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "doctor", "version": "0.0.1"}}}\n')
        proc.stdin.flush()
        
        r, _, _ = select.select([proc.stdout], [], [], 5.0)
        if not r:
            print("FAIL  mcp-handshake  Timeout waiting for initialize response")
            return False
            
        line = proc.stdout.readline()
        try:
            json.loads(line)
        except Exception as e:
            print(f"FAIL  mcp-handshake  Malformed initialize response: {e}")
            return False
            
        proc.stdin.write('{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        proc.stdin.write('{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n')
        proc.stdin.flush()
        
        r, _, _ = select.select([proc.stdout], [], [], 5.0)
        if not r:
            print("FAIL  mcp-handshake  Timeout waiting for tools/list response")
            return False
            
        line = proc.stdout.readline()
        try:
            tools_res = json.loads(line)
        except Exception as e:
            print(f"FAIL  mcp-handshake  Malformed tools/list response: {e}")
            return False
            
        actual_tools = {t.get("name") for t in tools_res.get("result", {}).get("tools", [])}
        expected_tools = get_server_tools()
        
        if actual_tools != expected_tools:
            missing = expected_tools - actual_tools
            extra = actual_tools - expected_tools
            msg = []
            if missing:
                msg.append(f"missing {missing}")
            if extra:
                msg.append(f"extra {extra}")
            print(f"FAIL  mcp-handshake  Tool sets differ: {', '.join(msg)}")
            return False
            
        print(f"OK  mcp-handshake  {len(actual_tools)} tools served correctly")
        return True
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

def check_hooks_exist():
    p = _get_settings_json_path()
    if not p.exists():
        print("WARN  hooks-exist  settings.json not found")
        return True
    
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"FAIL  hooks-exist  Unparseable settings.json: {e}")
        return False
        
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        print("OK  hooks-exist  No hooks defined")
        return True
        
    failures = []
    for hook_list in hooks.values():
        if not isinstance(hook_list, list):
            continue
        for hook in hook_list:
            if not isinstance(hook, dict) or hook.get("type") != "command":
                continue
            cmd = hook.get("command", "")
            matches = re.findall(r'(\S+\.py)', cmd)
            for m in matches:
                m = m.strip("'\"")
                if m.startswith("~"):
                    path_str = os.path.expanduser(m)
                else:
                    path_str = m
                path = Path(path_str)
                if not path.exists():
                    failures.append(f"Missing: {path} (from command: {cmd})")
                    continue
                
                try:
                    src = path.read_text(encoding="utf-8")
                    compile(src, str(path), "exec")
                except SyntaxError as e:
                    failures.append(f"SyntaxError in {path}: {e}")
                except Exception as e:
                    failures.append(f"Error reading {path}: {e}")
                    
    if failures:
        print("FAIL  hooks-exist  Missing or invalid hooks:")
        for f in failures:
            print(f"  - {f}")
        return False
        
    print("OK  hooks-exist  All referenced python hooks exist and compile")
    return True

def check_permissions_consistency():
    p = _get_settings_json_path()
    if not p.exists():
        print("WARN  permissions-consistency  settings.json not found")
        return True
        
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        print("FAIL  permissions-consistency  Unparseable settings.json")
        return False
        
    allowed = data.get("permissions", {}).get("allow", [])
    allow_set = set()
    for a in allowed:
        m = re.match(r'^mcp__ai-router__(\w+)$', a)
        if m:
            allow_set.add(m.group(1))
            
    expected = get_server_tools()
    
    if allow_set != expected:
        missing = expected - allow_set
        extra = allow_set - expected
        msg = []
        if missing:
            msg.append(f"served but not allow-listed: {missing}")
        if extra:
            msg.append(f"allow-listed but not served: {extra}")
        print(f"FAIL  permissions-consistency  Permissions mismatch: {', '.join(msg)}")
        return False
        
    print("OK  permissions-consistency  Permissions exactly match served tools")
    return True

def check_launchd(labels=None):
    if labels is None:
        labels = [
            "com.ai-router.rag-sweep",
            "com.su6i.vault-export",
            "com.su6i.rag-interval-review"
        ]
    uid = os.getuid()
    all_ok = True
    
    for label in labels:
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist_path.exists():
            print(f"WARN  launchd:{label}  plist not installed")
            continue
            
        try:
            res = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], 
                                 capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            print(f"FAIL  launchd:{label}  launchctl not found")
            all_ok = False
            continue
        except Exception as e:
            print(f"FAIL  launchd:{label}  launchctl error: {e}")
            all_ok = False
            continue
            
        if res.returncode != 0 or "could not find service" in res.stdout.lower() or "could not find service" in res.stderr.lower():
            print(f"FAIL  launchd:{label}  plist present but not loaded")
            all_ok = False
            continue
            
        m = re.search(r"last exit code = (-?\d+)", res.stdout)
        if m:
            code = int(m.group(1))
            if code == 0:
                print(f"OK  launchd:{label}  loaded and exited 0")
            else:
                print(f"FAIL  launchd:{label}  last exit code = {code}")
                all_ok = False
        else:
            print(f"OK  launchd:{label}  not yet run")
            
    return all_ok

def check_vault_env():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        import delegate
    except Exception as e:
        print(f"FAIL  vault-env  Failed to import delegate: {e}")
        return False
    finally:
        sys.path.pop(0)
        
    expected_keys = set()
    for spec in delegate.MODELS.values():
        key = spec.get("key")
        if key:
            expected_keys.add(key)
            
    # Mirrors delegate.py's _agent_projects_root()/_vault_root(): the shared
    # secrets dir always lives under agent-projects (XDG_DATA_HOME, else
    # <home>/.local/share), independent of any override; AI_ROUTER_DATA_DIR
    # (despite its name) overrides the VAULT root itself, not a data
    # subdirectory of it — see delegate.py's _vault_root().
    xdg = os.environ.get("XDG_DATA_HOME")
    agent_projects_root = Path(xdg).expanduser() if xdg else _get_home() / ".local" / "share" / "agent-projects"

    vault_override = os.environ.get("AI_ROUTER_DATA_DIR")
    vault_root = Path(vault_override).expanduser() if vault_override else agent_projects_root / "ai-router"

    env_files = [
        agent_projects_root / "_shared" / "secrets" / ".env",
        vault_root / "secrets" / ".env",
    ]
    
    present_keys = set()
    for f in env_files:
        if f.exists():
            try:
                content = f.read_text(encoding="utf-8")
                for line in content.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line:
                            k = line.split("=", 1)[0].strip()
                            present_keys.add(k)
            except Exception:
                pass
                
    missing = []
    for k in expected_keys:
        if k not in present_keys and k not in os.environ:
            missing.append(k)
            
    if missing:
        print(f"FAIL  vault-env  Missing expected secrets: {', '.join(missing)}")
        return False
        
    print("OK  vault-env  All expected API keys present")
    return True

def check_postgres():
    dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        # The DSN lives in the rule-035 vault layer, not the ambient
        # environment, so reading os.environ alone made this check report
        # "not set" on a machine whose vault does define it. A check that can
        # never fire is false assurance, not caution. load_env() lets a real
        # environment variable win over both .env layers (see delegate.py).
        sys.path.insert(0, str(REPO_ROOT / "src"))
        try:
            import delegate

            delegate.load_env()
        except Exception:
            pass
        finally:
            sys.path.pop(0)
        dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("WARN  postgres  POSTGRES_DSN not set in the environment or the vault — skipping")
        return True
        
    parsed = urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    
    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
        print("OK  postgres  Postgres reachable")
        return True
    except Exception:
        print("WARN  postgres  Postgres not reachable — start it first: colima start")
        return True

class CaptureOutput:
    def __init__(self):
        self.ok = 0
        self.warn = 0
        self.fail = 0
        
    def write(self, s):
        lines = s.splitlines()
        for line in lines:
            if line.startswith("OK "):
                self.ok += 1
            elif line.startswith("WARN "):
                self.warn += 1
            elif line.startswith("FAIL "):
                self.fail += 1
        sys.__stdout__.write(s)
        
    def flush(self):
        sys.__stdout__.flush()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Fix automatically fixable issues")
    args = parser.parse_args()
    
    if args.fix:
        fix_mcp_registration()
        
    cap = CaptureOutput()
    orig_stdout = sys.stdout
    sys.stdout = cap
    
    try:
        check_mcp_registration()
        check_mcp_handshake()
        check_hooks_exist()
        check_permissions_consistency()
        check_launchd()
        check_vault_env()
        check_postgres()
    finally:
        sys.stdout = orig_stdout
        
    print(f"\nSummary: {cap.ok} OK, {cap.warn} WARN, {cap.fail} FAIL")
    
    if cap.fail > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
