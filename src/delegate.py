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

Keys come from the vault .env (_shared, then this project's own override — rule 035
layered secrets). No key is printed in full.
Claude is intentionally NOT reachable here — grunt work never falls back to the
subscription. See STRATEGY.md (source of truth) for the routing policy.
"""
import argparse
import hashlib
import json
import os
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
            con.execute("UPDATE cache SET hits=hits+1 WHERE key=?", (key,)); con.commit()
        con.close(); return row[0] if row else None
    except Exception:
        return None      # fail-open: cache never breaks a call


def cache_put(key, model, prompt, response):
    try:
        con = _cache_conn()
        con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,0)",
                    (key, model, prompt, response,
                     dt.datetime.now().astimezone().isoformat(timespec="seconds")))
        con.commit(); con.close()
    except Exception:
        pass


def _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                  cache, cost, dt_s, cached=False):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as fh:
        fh.write(json.dumps({
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "model_asked": model, "model_echoed": echoed, "id": rid,
            "session": session or None, "project": project, "commit": commit,
            "in": pin, "out": pout, "cache": cache,
            "cost_usd": round(cost, 6), "latency_s": round(dt_s, 2),
            "cached": cached}) + "\n")


# ---- provider calls ----------------------------------------------------------
def call_openai(spec, key, history, system):
    msgs = ([{"role": "system", "content": system}] if system else []) + history
    r = httpx.post(f"{spec['url']}/chat/completions", timeout=180,
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": spec["api"], "messages": msgs, "max_tokens": 8192})
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"], d.get("model"), d.get("id"),
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
            (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))


def call_gemini(spec, key, history, system):
    # Gemini roles: user / model. Map our history (user/assistant) accordingly.
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in history]
    body = {"contents": contents}
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


def delegate(prompt: str, model: str, session: str = "", system: str = "",
             use_cache: bool = True) -> str:
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
                         0, 0, 0, 0.0, 0.0, cached=True)
            return hit

    history = load_history(session) if session else []
    history.append({"role": "user", "content": prompt})

    print(f"→ delegating to {model} ({spec['api']}) via {spec['url']}"
          + (f"  [session: {session}, {len(history)} msgs]" if session else ""))
    print(f"  key: {key[:6]}...{key[-4:]} (len={len(key)})")

    t0 = time.time()
    caller = call_gemini if spec["provider"] == "gemini" else call_openai
    answer, echoed, rid, pin, pout, cache = caller(spec, key, history, system)
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
                 cache, cost, dt_s, cached=False)
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
    a = ap.parse_args()

    if a.audit:
        show_audit(); return
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
