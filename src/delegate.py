#!/usr/bin/env python3
"""delegate.py — single LLM gateway for grunt-work, with PROOF + audit + memory.

Every call prints the model name ECHOED BY THE PROVIDER'S SERVER (not by us), the
response id, token usage and computed cost — then appends one line to audit.log so
you have an independent ledger. Optional conversation memory (--session) makes coding
iterative ("now add tests") instead of one-shot.

Providers:
  gemini  — Google Gemini/Gemma, FREE tier (cost $0), google endpoint
  minimax — MiniMax-M3 (prepaid, spend first)
  flash   — deepseek-v4-flash    pro — deepseek-v4-pro    grok — grok-4.3

Usage:
  python3 delegate.py -p "prompt"                       # default model = minimax
  python3 delegate.py --model gemini -p "..."           # free
  python3 delegate.py --model deepseek-v4-flash --plan PLAN.md --out ANSWER.md
  python3 delegate.py --model gemini --session code -p "write a fib()"   # remembers
  python3 delegate.py --model gemini --session code -p "now add memoization"
  python3 delegate.py --session code --new              # reset that conversation
  python3 delegate.py --audit                           # print the ledger

  # worker mode (SPEC v1) — cheap model reads/rewrites files on disk directly;
  # the generated code NEVER enters this process's stdout/context, only a
  # short summary does.
  python3 delegate.py --model flash --files "src/foo.py,tests/test_foo.py" \
      --allow-write "src/**,tests/**" --verify "uv run pytest -q" \
      -p "add a docstring to foo()"

Keys come from the vault .env (_shared, then this project's own override — rule 035
layered secrets). No key is printed in full.
Claude is intentionally NOT reachable here — grunt work never falls back to the
subscription. See STRATEGY.md (source of truth) for the routing policy.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path
import httpx


def _agent_projects_root() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "agent-projects"


def _vault_root() -> Path:
    """rule-035 resolver for THIS tool's own state (not the caller's cwd/project).

    delegate.py's cache/audit/sessions always live in the ai-router project vault,
    regardless of which repo invoked it (e.g. via the `r()` shell wrapper).
    """
    if override := os.environ.get("AI_ROUTER_DATA_DIR"):
        return Path(override).expanduser()
    return _agent_projects_root() / "ai-router"


AGENT_PROJECTS = _agent_projects_root()
VAULT = _vault_root()
DATA_DIR = VAULT / "data"
SECRETS_DIR = VAULT / "secrets"
AUDIT = DATA_DIR / "audit.log"
SESSIONS = DATA_DIR / "sessions"
CACHE = DATA_DIR / "cache.db"

# key -> provider spec. provider: "openai" (OpenAI-compatible) or "gemini".
# Priority (per STRATEGY.md): MiniMax first (prepaid, never recharged) → DeepSeek → Grok.
# Gemini is FREE ($0) but rate-limited (~a few req) — good for light chat/code one-shots.
MODELS = {
    "minimax": dict(api="MiniMax-M3",        provider="openai", url="https://api.minimax.io/v1",
                    cin=0.30, cout=1.20, key="MINIMAX_API_KEY"),
    "flash":   dict(api="deepseek-v4-flash", provider="openai", url="https://api.deepseek.com/v1",
                    cin=0.14, cout=0.28, key="DEEPSEEK_API_KEY"),
    "pro":     dict(api="deepseek-v4-pro",   provider="openai", url="https://api.deepseek.com/v1",
                    cin=0.435, cout=0.87, key="DEEPSEEK_API_KEY"),
    "grok":    dict(api="grok-4.3",          provider="openai", url="https://api.x.ai/v1",
                    cin=1.25, cout=2.50, key="GROK_API_KEY"),
    "gemini":  dict(api="gemini-2.5-flash",  provider="gemini",
                    url="https://generativelanguage.googleapis.com/v1beta",
                    cin=0.0, cout=0.0, key="GEMINI_API_KEY"),
    "gemini-lite": dict(api="gemini-2.5-flash-lite", provider="gemini",
                    url="https://generativelanguage.googleapis.com/v1beta",
                    cin=0.0, cout=0.0, key="GEMINI_API_KEY"),
    "gemma":   dict(api="gemma-4-31b-it", provider="gemini",
                    url="https://generativelanguage.googleapis.com/v1beta",
                    cin=0.0, cout=0.0, key="GEMINI_API_KEY"),
}

# Friendly aliases -> canonical key.
ALIASES = {
    "minimax": "minimax", "minimax-m3": "minimax", "m3": "minimax",
    "flash": "flash", "deepseek": "flash", "ds": "flash", "deepseek-v4": "flash",
    "deepseek-flash": "flash", "deepseek-v4-flash": "flash",
    "pro": "pro", "reasoner": "pro", "deepseek-pro": "pro", "deepseek-v4-pro": "pro",
    "grok": "grok", "grok-4.3": "grok", "grok4": "grok",
    "gemini": "gemini", "gemini-2.5-flash": "gemini", "flash-gemini": "gemini",
    "gemini-lite": "gemini-lite", "gemini-2.5-flash-lite": "gemini-lite", "lite": "gemini-lite",
    "gemma": "gemma", "gemma-4": "gemma", "gemma-4-31b-it": "gemma", "gemma3": "gemma",
}


def resolve_model(name: str) -> str:
    key = ALIASES.get(name.strip().lower())
    if key is None:
        sys.exit(f"❌ unknown model '{name}'. Known: {', '.join(sorted(ALIASES))}")
    return key


def load_env():
    # Layered secrets (rule 035): shared keys first, this project's own overrides second.
    for f in (AGENT_PROJECTS / "_shared" / "secrets" / ".env", SECRETS_DIR / ".env"):
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()   # project secrets override shared


def project_info():
    """Best-effort (project, commit) for the CWD where `amir router` was invoked."""
    cwd = os.getcwd()
    def git(*a):
        try:
            r = subprocess.run(["git", *a], cwd=cwd, capture_output=True,
                               text=True, timeout=3)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""
    remote = git("config", "--get", "remote.origin.url")
    project = remote.rstrip("/").split("/")[-1].removesuffix(".git") if remote else ""
    if not project:
        top = git("rev-parse", "--show-toplevel")
        project = os.path.basename(top) if top else os.path.basename(cwd)
    return (project or os.path.basename(cwd), git("rev-parse", "--short", "HEAD") or None)


def show_audit():
    print(AUDIT.read_text().rstrip() if AUDIT.exists() else "(no audit.log yet)")


# ---- conversation memory -----------------------------------------------------
def load_history(session: str) -> list:
    f = SESSIONS / f"{session}.json"
    return json.loads(f.read_text()) if f.exists() else []


def save_history(session: str, history: list):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{session}.json").write_text(json.dumps(history, ensure_ascii=False, indent=1))


# ---- exact-hash cache (playbook #13 — deterministic only, no semantic cache) --
def _norm(s):
    return " ".join((s or "").split())


def cache_make_key(model, system, prompt):
    raw = f"{model}\x00{_norm(system)}\x00{_norm(prompt)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_conn():
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE, timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, model TEXT,"
                " prompt TEXT, response TEXT, created TEXT, hits INTEGER DEFAULT 0)")
    return con


def cache_get(key):
    try:
        con = _cache_conn()
        row = con.execute("SELECT response FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            con.execute("UPDATE cache SET hits=hits+1 WHERE key=?", (key,))
            con.commit()
        con.close()
        return row[0] if row else None
    except Exception:
        return None      # fail-open: cache never breaks a call


def cache_put(key, model, prompt, response):
    try:
        con = _cache_conn()
        con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,0)",
                    (key, model, prompt, response,
                     dt.datetime.now().astimezone().isoformat(timespec="seconds")))
        con.commit()
        con.close()
    except Exception:
        pass


def _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                  cache, cost, dt_s, cached=False, via=None):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_asked": model, "model_echoed": echoed, "id": rid,
        "session": session or None, "project": project, "commit": commit,
        "in": pin, "out": pout, "cache": cache,
        "cost_usd": round(cost, 6), "latency_s": round(dt_s, 2),
        "cached": cached,
    }
    if via is not None:
        rec["via"] = via
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


# ---- provider calls ----------------------------------------------------------
def call_openai(spec, key, history, system, max_output_tokens: int = 8192):
    msgs = ([{"role": "system", "content": system}] if system else []) + history
    r = httpx.post(f"{spec['url']}/chat/completions", timeout=180,
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": spec["api"], "messages": msgs, "max_tokens": max_output_tokens})
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"], d.get("model"), d.get("id"),
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
            (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))


def call_gemini(spec, key, history, system, max_output_tokens: int = 8192):
    # Gemini roles: user / model. Map our history (user/assistant) accordingly.
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in history]
    body = {"contents": contents, "generationConfig": {"maxOutputTokens": max_output_tokens}}
    if system:
        if spec["api"].startswith("gemma") and contents:
            # Gemma models reject systemInstruction; fold it into the first user turn
            contents[0]["parts"][0]["text"] = system + "\n\n" + contents[0]["parts"][0]["text"]
        else:
            body["systemInstruction"] = {"parts": [{"text": system}]}
    r = httpx.post(f"{spec['url']}/models/{spec['api']}:generateContent?key={key}",
                   timeout=180, headers={"Content-Type": "application/json"}, json=body)
    r.raise_for_status()
    d = r.json()
    um = d.get("usageMetadata", {})
    text = d["candidates"][0]["content"]["parts"][0]["text"]
    return (text, d.get("modelVersion", spec["api"]), d.get("responseId"),
            um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0),
            um.get("cachedContentTokenCount", 0))


# ---- worker mode (--files) — SPEC v1, DELEGATE-TOOL-DESIGN.md ---------------
# Sentinel-line protocol (not markdown fences: file content may itself contain
# backticks). Full-file replacement only — cheap models are unreliable with diffs.
WORKER_PROTOCOL_SYSTEM = """You are a coding worker. You are given a task and the \
current content of one or more files. Make the requested change and respond using \
EXACTLY this format — no markdown code fences, no commentary outside these markers:

===FILE: relative/path/from/project/root.py===
<entire new file content — full replacement, never a diff or patch>
===END FILE===
(repeat the FILE block for every file you changed)
===SUMMARY===
3-5 lines: what was done, what was NOT done, any assumption you made.
===END SUMMARY===

Rules:
- Always emit the FULL file content, never a partial diff.
- Only emit FILE blocks for files you are actually changing.
- Never wrap file content in markdown fences.
- Paths are relative to the project root: no leading slash, no ".." segments.
"""

_FILE_START_RE = re.compile(r"^===FILE: (.+)===$")
_FILE_END = "===END FILE==="
_SUMMARY_START = "===SUMMARY==="
_SUMMARY_END = "===END SUMMARY==="


def parse_worker_response(text: str):
    """Parse sentinel-line blocks. Returns (files: list[(path, content)], summary: str|None).

    Regex on line starts per SPEC v1 — content between markers is written verbatim
    (a trailing newline is added by the caller if missing, never here).
    """
    lines = text.split("\n")
    files, summary = [], None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip("\r")
        m = _FILE_START_RE.match(line)
        if m:
            path = m.group(1).strip()
            i += 1
            body = []
            while i < n and lines[i].rstrip("\r") != _FILE_END:
                body.append(lines[i])
                i += 1
            files.append((path, "\n".join(body)))
            i += 1  # skip ===END FILE===
            continue
        if line == _SUMMARY_START:
            i += 1
            body = []
            while i < n and lines[i].rstrip("\r") != _SUMMARY_END:
                body.append(lines[i])
                i += 1
            summary = "\n".join(body).strip()
            i += 1
            continue
        i += 1
    return files, summary


def _safe_write_path(rel: str, project_root: Path, allow_patterns: list):
    """Path safety per SPEC v1. Returns (resolved_path, None) or (None, reason)."""
    if not rel:
        return None, "empty path"
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return None, "absolute path"
    if ".." in norm.split("/"):
        return None, "path traversal (..)"
    if not allow_patterns:
        return None, "no --allow-write patterns given"
    if not any(fnmatch.fnmatch(norm, pat) for pat in allow_patterns):
        return None, "not covered by --allow-write"
    root = project_root.resolve()
    candidate = (root / norm).resolve()
    if candidate != root and root not in candidate.parents:
        return None, "escapes project root"
    return candidate, None


def _write_files(files: list, project_root: Path, allow_patterns: list):
    written, rejected = [], []
    for rel, content in files:
        path, err = _safe_write_path(rel, project_root, allow_patterns)
        if err:
            rejected.append((rel, err))
            continue
        data = content if content.endswith("\n") else content + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        written.append((rel, len(data.encode())))
    return written, rejected


def _human_size(n: int) -> str:
    return f"{n}b" if n < 1024 else f"{n / 1024:.1f}k"


def _tail_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def run_verify(cmd: str, cwd: Path):
    """Run --verify. Output is captured, NEVER printed in full. Returns (ok, output, elapsed_s)."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=600)
        ok = r.returncode == 0
        output = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        ok, output = False, "TIMEOUT after 600s"
    return ok, output, time.time() - t0


def build_worker_prompt(task: str, file_specs: list) -> str:
    parts = [f"Task:\n{task}\n"]
    for path, content in file_specs:
        parts.append(f"===CURRENT FILE: {path}===\n{content}\n===END CURRENT FILE===\n")
    return "\n".join(parts)


def _format_worker_summary(written, rejected, verify_cmd, verify_status, attempt,
                            max_attempts, elapsed, summary, total_files, cost,
                            echoed_model, fail_tail):
    def fmt_written(items):
        return ", ".join(f"{p} ({_human_size(sz)})" for p, sz in items) if items else "(none)"

    def fmt_rejected(items):
        return ", ".join(f"REJECTED: {p} ({reason})" for p, reason in items) if items else "(none)"

    lines = [
        f"files written : {fmt_written(written)}",
        f"rejected      : {fmt_rejected(rejected)}",
    ]
    if verify_cmd:
        v = f"verify        : {verify_cmd} → {verify_status}"
        if verify_status != "SKIPPED":
            v += f" ({elapsed:.1f}s)   [attempt {attempt}/{max_attempts}]"
        lines.append(v)
    else:
        lines.append("verify        : (skipped — no --verify given)")
    lines.append(f"worker summary: {summary or f'worker returned {total_files} files, no summary'}")
    lines.append(f"cost          : ${cost:.6f} · model echoed: {echoed_model}")
    if verify_status == "FAIL" and fail_tail:
        lines.append("")
        lines.append("verify output (last 15 lines):")
        lines.append(_tail_lines(fail_tail, 15))
    return "\n".join(lines)


def worker_delegate(task: str, model: str, files_arg: str, allow_write_arg: str,
                     verify_cmd: str, retries: int, project_root: Path = None,
                     via: str | None = None) -> str:
    """Worker mode per DELEGATE-TOOL-DESIGN.md SPEC v1. Only the returned summary
    (≤25 lines) is meant to reach Claude's context — golden rule."""
    spec = MODELS[model]
    key = os.environ.get(spec["key"], "")
    if not key:
        sys.exit(f"❌ {spec['key']} not set in vault .env")

    project_root = project_root or Path.cwd()
    rel_files = [f.strip() for f in files_arg.split(",") if f.strip()] if files_arg else []
    allow_patterns = [p.strip() for p in allow_write_arg.split(",") if p.strip()] if allow_write_arg else []
    max_attempts = min(max(retries, 0), 2) + 1

    file_specs = []
    for rel in rel_files:
        p = project_root / rel
        content = p.read_text() if p.exists() else "(file does not exist yet)"
        file_specs.append((rel, content))

    caller = call_gemini if spec["provider"] == "gemini" else call_openai
    history = [{"role": "user", "content": build_worker_prompt(task, file_specs)}]
    total_cost = 0.0
    echoed_model = spec["api"]

    def call_once():
        nonlocal total_cost, echoed_model
        answer, echoed, rid, pin, pout, _ = caller(spec, key, history, WORKER_PROTOCOL_SYSTEM)
        total_cost += pin / 1e6 * spec["cin"] + pout / 1e6 * spec["cout"]
        echoed_model = echoed or echoed_model
        return answer

    answer = call_once()
    files, summary = parse_worker_response(answer)
    if not files:
        # Protocol failure: exactly one automatic re-prompt, then fail loudly.
        history.append({"role": "assistant", "content": answer})
        history.append({"role": "user",
                        "content": "your output did not follow the FILE protocol, re-emit"})
        answer = call_once()
        files, summary = parse_worker_response(answer)
        if not files:
            sys.exit("❌ worker returned no ===FILE=== blocks after one re-prompt "
                     "— protocol failure")
    history.append({"role": "assistant", "content": answer})

    written, rejected = _write_files(files, project_root, allow_patterns)
    total_files = len(files)

    attempt = 1
    verify_status, elapsed, fail_output = "SKIPPED", 0.0, ""
    if verify_cmd:
        while True:
            ok, output, elapsed = run_verify(verify_cmd, project_root)
            verify_status = "PASS" if ok else "FAIL"
            if ok or attempt >= max_attempts:
                fail_output = output if not ok else ""
                break
            history.append({"role": "user",
                            "content": f"verify failed:\n{_tail_lines(output, 40)}\n"
                                       f"fix the files and re-emit the full FILE protocol."})
            attempt += 1
            answer = call_once()
            history.append({"role": "assistant", "content": answer})
            retry_files, retry_summary = parse_worker_response(answer)
            if retry_summary:
                summary = retry_summary
            if retry_files:
                more_written, more_rejected = _write_files(retry_files, project_root, allow_patterns)
                written.extend(more_written)
                rejected.extend(more_rejected)
                total_files += len(retry_files)

    project, commit = project_info()
    _write_worker_audit(model, echoed_model, project, commit, written, rejected,
                        verify_cmd, verify_status, attempt, total_cost, via=via)

    return _format_worker_summary(written, rejected, verify_cmd, verify_status, attempt,
                                  max_attempts, elapsed, summary, total_files, total_cost,
                                  echoed_model, fail_output)


def _write_worker_audit(model, echoed, project, commit, written, rejected,
                        verify_cmd, verify_status, attempts, cost, via=None):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_asked": model, "model_echoed": echoed,
        "session": None, "project": project, "commit": commit,
        "cost_usd": round(cost, 6), "cached": False,
        "mode": "worker",
        "files_written": [p for p, _ in written],
        "files_rejected": [p for p, _ in rejected],
        "verify_cmd": verify_cmd, "verify_status": verify_status,
        "attempts": attempts,
    }
    if via is not None:
        rec["via"] = via
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def delegate(prompt: str, model: str, session: str = "", system: str = "",
             use_cache: bool = True, max_output_tokens: int = 8192,
             via: str | None = None) -> str:
    spec = MODELS[model]
    key = os.environ.get(spec["key"], "")
    if not key:
        sys.exit(f"❌ {spec['key']} not set in vault .env")

    project, commit = project_info()

    # Exact-hash cache: only for stateless one-shots (a --session call is a
    # multi-turn conversation, never safe to serve from a single cached turn).
    cache_key = cache_make_key(model, system, prompt) if (use_cache and not session) else None
    if cache_key:
        hit = cache_get(cache_key)
        if hit is not None:
            print(f"⚡ cache HIT ({model}, {spec['api']}) — $0.000000, 0.00s")
            _write_audit(model, spec["api"], None, session, project, commit,
                         0, 0, 0, 0.0, 0.0, cached=True, via=via)
            return hit

    history = load_history(session) if session else []
    history.append({"role": "user", "content": prompt})

    print(f"→ delegating to {model} ({spec['api']}) via {spec['url']}"
          + (f"  [session: {session}, {len(history)} msgs]" if session else ""))
    print(f"  key: {key[:6]}...{key[-4:]} (len={len(key)})")

    t0 = time.time()
    caller = call_gemini if spec["provider"] == "gemini" else call_openai
    answer, echoed, rid, pin, pout, cache = caller(
        spec, key, history, system, max_output_tokens=max_output_tokens)
    dt_s = time.time() - t0
    cost = pin / 1e6 * spec["cin"] + pout / 1e6 * spec["cout"]

    print("\n===== PROOF (from provider's server) =====")
    print("model echoed :", echoed)
    print("response id  :", rid)
    print("usage        : in=%d out=%d cache=%d" % (pin, pout, cache))
    print("cost         : $%.6f%s" % (cost, "  (FREE tier)" if spec["cin"] == 0 else ""))
    print("latency      : %.2fs" % dt_s)
    print("==========================================\n")

    if session:
        history.append({"role": "assistant", "content": answer})
        save_history(session, history)

    if cache_key:
        cache_put(cache_key, model, prompt, answer)

    _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                 cache, cost, dt_s, cached=False, via=via)
    return answer


def main():
    ap = argparse.ArgumentParser(description="Delegate a task to a grunt/free model, with proof + memory.")
    ap.add_argument("-p", "--prompt")
    ap.add_argument("--plan", help="read the prompt from this file")
    ap.add_argument("--out", help="write the answer to this file (else stdout)")
    ap.add_argument("--model", default="minimax",
                    help="model or alias (minimax|flash|pro|grok|gemini or full names)")
    ap.add_argument("--session", default="", help="conversation name to remember across calls")
    ap.add_argument("--new", action="store_true", help="reset the named session before running")
    ap.add_argument("--system", default="", help="system instruction (persona / rules)")
    ap.add_argument("--audit", action="store_true", help="print the delegation ledger and exit")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the exact-hash cache (always call the provider)")
    ap.add_argument("--files", default="",
                    help="worker mode: comma-separated files to read/rewrite")
    ap.add_argument("--allow-write", default="",
                    help="worker mode: comma-separated globs (relative to cwd) the "
                         "worker is allowed to write; no flag = no writes")
    ap.add_argument("--verify", default="",
                    help="worker mode: shell command run after writing (never guessed)")
    ap.add_argument("--retries", type=int, default=1,
                    help="worker mode: verify-failure retries (default 1, max 2)")
    a = ap.parse_args()

    if a.audit:
        show_audit()
        return
    if a.new and a.session:
        f = SESSIONS / f"{a.session}.json"
        f.exists() and f.unlink()
        print(f"↺ session '{a.session}' reset")
        if not (a.prompt or a.plan):
            return

    load_env()
    prompt = Path(a.plan).read_text() if a.plan else a.prompt
    if not prompt:
        sys.exit("❌ need -p PROMPT or --plan FILE (or --audit / --new)")

    model = resolve_model(a.model)

    if a.files:
        print(worker_delegate(prompt, model, a.files, a.allow_write, a.verify, a.retries))
        return

    use_cache = not a.no_cache
    try:
        answer = delegate(prompt, model, a.session, a.system, use_cache=use_cache)
    except httpx.HTTPStatusError as e:
        # MiniMax is prepaid and NEVER recharged; on 401/402/429 fall back to DeepSeek.
        if model == "minimax" and e.response.status_code in (401, 402, 429):
            print(f"⚠️  MiniMax failed (HTTP {e.response.status_code}) — credit likely "
                  f"exhausted. Per policy MiniMax is NOT recharged; falling back to "
                  f"DeepSeek flash. Make 'flash' your default from now on.", file=sys.stderr)
            answer = delegate(prompt, "flash", a.session, a.system, use_cache=use_cache)
        else:
            raise

    if a.out:
        Path(a.out).write_text(answer)
        print(f"answer written → {a.out} ({len(answer)} chars)")
    else:
        print(answer)


if __name__ == "__main__":
    main()
