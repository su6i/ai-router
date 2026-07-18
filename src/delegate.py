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
import logging
import time
import unicodedata
import datetime as dt
from pathlib import Path
import httpx

HTTP_TIMEOUT = 180
VERIFY_TIMEOUT = 600
GIT_TIMEOUT = 3
SQLITE_TIMEOUT = 5
CACHE_MAX_ROWS = 5000
CACHE_MAX_AGE_DAYS = 90

logger = logging.getLogger("ai_router")

class ProviderError(Exception):
    def __init__(self, model, status, short_reason):
        super().__init__(f"{model} failed: HTTP {status} ({short_reason})")
        self.model = model
        self.status = status
        self.short_reason = short_reason

def _post_with_retry(model, *args, **kwargs):
    kwargs["timeout"] = HTTP_TIMEOUT
    max_attempts = 3
    sleeps = [1, 3]
    for attempt in range(max_attempts):
        try:
            r = httpx.post(*args, **kwargs)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                status = r.status_code
                reason = r.reason_phrase
            else:
                raise ProviderError(model, r.status_code, r.reason_phrase)
        except httpx.TimeoutException:
            status = "TIMEOUT"
            reason = "timeout"
        except httpx.RequestError as e:
            status = "NETWORK_ERROR"
            reason = type(e).__name__

        if attempt < max_attempts - 1:
            time.sleep(sleeps[attempt])
        else:
            raise ProviderError(model, status, reason)


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
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
SECRETS_DIR = VAULT / "secrets"
AUDIT = DATA_DIR / "audit.log"
BUDGETS = DATA_DIR / "budgets.json"
SESSIONS = DATA_DIR / "sessions"
CACHE = DATA_DIR / "cache.db"

# key -> provider spec. provider: "openai" (OpenAI-compatible) or "gemini".
# Priority (per STRATEGY.md): MiniMax first (prepaid, never recharged) → DeepSeek → Grok.
# Gemini is FREE ($0) but rate-limited (~a few req) — good for light chat/code one-shots.
# cin_cached provenance: minimax 0.06 = owner's real billing (2026-07-13);
# deepseek 0.014/0.0435 = assumed 10x cache-hit discount (research 2026-07-11,
# official page lists no v4 models) — verify against real DeepSeek billing.
MODELS = {
    "minimax": dict(api="MiniMax-M3",        provider="openai", url="https://api.minimax.io/v1",
                    cin=0.30, cin_cached=0.06, cout=1.20, key="MINIMAX_API_KEY", quota_channel="minimax-api"),
    "flash":   dict(api="deepseek-v4-flash", provider="openai", url="https://api.deepseek.com/v1",
                    cin=0.14, cin_cached=0.014, cout=0.28, key="DEEPSEEK_API_KEY", quota_channel="deepseek-api"),
    "pro":     dict(api="deepseek-v4-pro",   provider="openai", url="https://api.deepseek.com/v1",
                    cin=0.435, cin_cached=0.0435, cout=0.87, key="DEEPSEEK_API_KEY", quota_channel="deepseek-api"),
    "grok":    dict(api="grok-4.3",          provider="openai", url="https://api.x.ai/v1",
                    cin=1.25, cout=2.50, key="GROK_API_KEY", quota_channel="grok-api"),
    "gemini":  dict(api="gemini-2.5-flash",  provider="gemini",
                    url="https://generativelanguage.googleapis.com/v1beta",
                    cin=0.0, cout=0.0, key="GEMINI_API_KEY", quota_channel="gemini-free"),
    "gemini-lite": dict(api="gemini-2.5-flash-lite", provider="gemini",
                    url="https://generativelanguage.googleapis.com/v1beta",
                    cin=0.0, cout=0.0, key="GEMINI_API_KEY", quota_channel="gemini-free"),
    "gemma":   dict(api="gemma-4-31b-it", provider="gemini",
                    url="https://generativelanguage.googleapis.com/v1beta",
                    cin=0.0, cout=0.0, key="GEMINI_API_KEY", quota_channel="gemini-free"),
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



def is_channel_enabled(channel: str) -> bool:
    env_disabled = os.environ.get("AI_ROUTER_DISABLE_CHANNELS", "")
    if channel in [c.strip() for c in env_disabled.split(",") if c.strip()]:
        return False
    channels_json = DATA_DIR / "channels.json"
    if not channels_json.exists():
        return True
    try:
        import json
        data = json.loads(channels_json.read_text())
        if channel in data:
            return bool(data[channel].get("enabled", True))
    except Exception:
        pass
    return True

def get_model_channel(model: str) -> str:
    spec = MODELS.get(resolve_model(model))
    if not spec:
        return model
    qc = spec.get("quota_channel", "")
    if qc.endswith("-api"):
        return qc[:-4]
    return qc


def resolve_model(name: str) -> str:
    key = ALIASES.get(name.strip().lower())
    if key is None:
        raise ValueError(f"unknown model '{name}'. Known: {', '.join(sorted(ALIASES))}")
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
                               text=True, timeout=GIT_TIMEOUT)
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


def check_budget(project: str, session: str, estimate_cost: float = 0.0, print_estimate: bool = False, model_spec: dict = None):
    has_budgets = BUDGETS.exists()
    if not has_budgets:
        if not print_estimate:
            logger.info("⚠️  no budgets.json — spend uncapped")
        budgets = {}
    else:
        try:
            budgets = json.loads(BUDGETS.read_text())
            if not budgets and not print_estimate:
                logger.info("⚠️  no budgets.json — spend uncapped")
        except Exception:
            if not print_estimate:
                logger.info("⚠️  budgets.json is invalid JSON — spend uncapped")
            budgets = {}

    monthly_cap = budgets.get("monthly_usd")
    weekly_cap = budgets.get("weekly_usd")
    session_cap = budgets.get("per_session_usd")
    project_caps = budgets.get("per_project_monthly_usd", {})
    project_cap = project_caps.get(project) if project else None

    now = dt.datetime.now().astimezone()
    month_str = now.isoformat()[:7]
    today_str = now.isoformat()[:10]
    week_ago = (now - dt.timedelta(days=7)).isoformat()

    spent_month = 0.0
    spent_week = 0.0
    spent_session = 0.0
    spent_project = 0.0
    spent_premium = 0
    copilot_monthly = budgets.get("copilot_premium_requests_month")
    daily_calls_count = {}

    if AUDIT.exists():
        with AUDIT.open("r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                
                ts = rec.get("ts", "")
                channel = rec.get("quota_channel")
                
                # Cache HITs never reached the provider — they consume no quota.
                if ts.startswith(today_str) and channel and not rec.get("cached"):
                    daily_calls_count[channel] = daily_calls_count.get(channel, 0) + 1
                    
                cost = rec.get("cost_usd", 0.0)
                if not cost:
                    continue
                
                if ts.startswith(month_str):
                    spent_month += cost
                    spent_premium += rec.get("premium_requests", 0)
                    if project and rec.get("project") == project:
                        spent_project += cost
                if ts >= week_ago:
                    spent_week += cost
                if session and rec.get("session") == session:
                    spent_session += cost

    if print_estimate:
        print("  Current month spend vs caps:")
        if monthly_cap is not None:
            print(f"    monthly_usd: ${spent_month:.6f} / ${monthly_cap:.2f}")
        else:
            print(f"    monthly_usd: ${spent_month:.6f} / (uncapped)")
        
        print("  Other caps:")
        if weekly_cap is not None:
            print(f"    weekly_usd : ${spent_week:.6f} / ${weekly_cap:.2f}")
        else:
            print(f"    weekly_usd : ${spent_week:.6f} / (uncapped)")
            
        if copilot_monthly is not None:
            print(f"    copilot_premium: {spent_premium} / {copilot_monthly}")
            
        if session:
            if session_cap is not None:
                print(f"    session_usd: ${spent_session:.6f} / ${session_cap:.2f}")
            else:
                print(f"    session_usd: ${spent_session:.6f} / (uncapped)")
                
        daily_calls_caps = budgets.get("daily_calls", {})
        if daily_calls_count or daily_calls_caps:
            print("  Daily calls vs caps:")
            for ch in set(list(daily_calls_count.keys()) + list(daily_calls_caps.keys())):
                count = daily_calls_count.get(ch, 0)
                cap = daily_calls_caps.get(ch)
                if cap is not None:
                    print(f"    {ch}: {count} / {cap}")
                else:
                    print(f"    {ch}: {count} / (uncapped)")
                
        sys.exit(0)

    # Apply estimate to actual spend
    spent_month += estimate_cost
    spent_week += estimate_cost
    if session:
        spent_session += estimate_cost
    if project:
        spent_project += estimate_cost

    def _check(name, spent, cap):
        if cap is not None:
            if spent > cap:
                if model_spec and model_spec.get("cin") == 0 and model_spec.get("cout") == 0:
                    logger.warning(f"⚠️  BUDGET WARNING: {name} cap exceeded (${spent:.6f} > ${cap:.2f}) but proceeding because model is FREE.")
                else:
                    sys.exit(f"❌ BUDGET ABORT: {name} cap exceeded (${spent:.6f} > ${cap:.2f})")
            elif spent >= cap * 0.8:
                logger.warning(f"⚠️  BUDGET WARNING: {name} spend at ${spent:.6f} (cap: ${cap:.2f})")

    _check("monthly_usd", spent_month, monthly_cap)
    _check("weekly_usd", spent_week, weekly_cap)
    if project:
        _check(f"per_project_monthly_usd[{project}]", spent_project, project_cap)
    if session:
        _check("per_session_usd", spent_session, session_cap)

    # Check quota channel daily call caps
    daily_calls_caps = budgets.get("daily_calls", {})
    current_channel = model_spec.get("quota_channel") if model_spec else None
    if current_channel:
        cap = daily_calls_caps.get(current_channel)
        count = daily_calls_count.get(current_channel, 0) + 1
        if cap is not None:
            if count > cap:
                sys.exit(f"❌ BUDGET ABORT: daily call cap exceeded for {current_channel} ({count} > {cap})")
            elif count >= cap * 0.8:
                logger.warning(f"⚠️  BUDGET WARNING: {current_channel} daily calls at {count} (cap: {cap})")

    if copilot_monthly is not None:
        if spent_premium > copilot_monthly:
            sys.exit(f"❌ BUDGET ABORT: copilot premium requests cap exceeded ({spent_premium} > {copilot_monthly})")
        elif spent_premium >= copilot_monthly * 0.8:
            logger.warning(f"⚠️  BUDGET WARNING: copilot premium requests at {spent_premium} (cap: {copilot_monthly})")


def show_cost(since: str = None, by: str = "model"):
    if not AUDIT.exists():
        print("(no audit.log yet)")
        return

    import collections
    groups = collections.defaultdict(lambda: {
        "calls": 0, "cached_hits": 0, "in_tokens": 0, "out_tokens": 0,
        "cache_tokens": 0, "cost_usd": 0.0, "has_tokens": False
    })

    malformed = 0
    copilot_premium_month = 0
    today_str = dt.datetime.now().astimezone().isoformat()[:10]
    month_str = today_str[:7]
    today_channel_calls = collections.defaultdict(int)
    with AUDIT.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                malformed += 1
                continue

            ts = rec.get("ts", "")
            channel = rec.get("quota_channel")
            if ts.startswith(month_str):
                copilot_premium_month += rec.get("premium_requests", 0)
                
            if ts.startswith(today_str) and channel and not rec.get("cached"):
                today_channel_calls[channel] += 1
            if since and ts[:10] < since:
                continue

            if by == "day":
                group_val = ts[:10]
            elif by == "model":
                group_val = rec.get("model_asked") or rec.get("model")
            else:
                group_val = rec.get(by)
            
            group_val = str(group_val) if group_val is not None else ""
            if not group_val:
                group_val = "(none)"

            g = groups[group_val]
            g["calls"] += 1
            if rec.get("cached"):
                g["cached_hits"] += 1

            g["cost_usd"] += rec.get("cost_usd", 0.0)

            if rec.get("mode") != "worker" and "in" in rec:
                g["has_tokens"] = True
                g["in_tokens"] += rec.get("in", 0)
                g["out_tokens"] += rec.get("out", 0)
                g["cache_tokens"] += rec.get("cache", 0)

    def fmt_int(v, has_tokens):
        return str(v) if has_tokens else ""

    def fmt_hit_rate(cache, in_tok, has_tokens):
        if not has_tokens or in_tok == 0:
            return ""
        return f"{cache / in_tok * 100:.1f}%"

    rows = []
    tot = {
        "calls": 0, "cached_hits": 0, "in_tokens": 0, "out_tokens": 0,
        "cache_tokens": 0, "cost_usd": 0.0, "has_tokens": False
    }

    for k, g in sorted(groups.items()):
        if g["has_tokens"]:
            tot["has_tokens"] = True
            tot["in_tokens"] += g["in_tokens"]
            tot["out_tokens"] += g["out_tokens"]
            tot["cache_tokens"] += g["cache_tokens"]
        tot["calls"] += g["calls"]
        tot["cached_hits"] += g["cached_hits"]
        tot["cost_usd"] += g["cost_usd"]

        rows.append([
            k,
            str(g["calls"]),
            str(g["cached_hits"]),
            fmt_int(g["in_tokens"], g["has_tokens"]),
            fmt_int(g["out_tokens"], g["has_tokens"]),
            fmt_int(g["cache_tokens"], g["has_tokens"]),
            fmt_hit_rate(g["cache_tokens"], g["in_tokens"], g["has_tokens"]),
            f"{g['cost_usd']:.6f}"
        ])

    rows.append([
        "TOTAL",
        str(tot["calls"]),
        str(tot["cached_hits"]),
        fmt_int(tot["in_tokens"], tot["has_tokens"]),
        fmt_int(tot["out_tokens"], tot["has_tokens"]),
        fmt_int(tot["cache_tokens"], tot["has_tokens"]),
        fmt_hit_rate(tot["cache_tokens"], tot["in_tokens"], tot["has_tokens"]),
        f"{tot['cost_usd']:.6f}"
    ])

    headers = ["group", "calls", "cached_hits", "in_tokens", "out_tokens", "cache_tokens", "hit_rate", "cost_usd"]
    widths = [max(len(str(item)) for item in col) for col in zip(headers, *rows)]

    def fmt_row(r):
        res = [r[0].ljust(widths[0])]
        for item, w in zip(r[1:], widths[1:]):
            res.append(item.rjust(w))
        return "  ".join(res)

    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows[:-1]:
        print(fmt_row(r))
    print("  ".join("-" * w for w in widths))
    print(fmt_row(rows[-1]))

    if today_channel_calls:
        caps = {}
        if BUDGETS.exists():
            try:
                caps = json.loads(BUDGETS.read_text()).get("daily_calls", {})
            except Exception:
                pass
        print(f"\ntoday's calls per quota channel ({today_str}):")
        for ch in sorted(today_channel_calls):
            cap = caps.get(ch)
            print(f"  {ch}: {today_channel_calls[ch]}" + (f" / {cap}" if cap is not None else " / (uncapped)"))

    if malformed > 0:
        print(f"\nskipped {malformed} malformed lines")
        
    if copilot_premium_month > 0:
        print(f"\nCopilot premium requests (this month): {copilot_premium_month}")


# ---- conversation memory -----------------------------------------------------
def load_history(session: str) -> list:
    f = SESSIONS / f"{session}.json"
    return json.loads(f.read_text()) if f.exists() else []


def save_history(session: str, history: list):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{session}.json").write_text(json.dumps(history, ensure_ascii=False, indent=1))


# ---- exact-hash cache (playbook #13 — deterministic only, no semantic cache) --
def _norm(s):
    if not s:
        return ""
    return " ".join(unicodedata.normalize("NFC", s).split())


def cache_make_key(model, system, prompt, max_output_tokens):
    raw = f"{model}\x00{_norm(system)}\x00{_norm(prompt)}\x00{max_output_tokens}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_conn():
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE, timeout=SQLITE_TIMEOUT)
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
        cache_prune()
    except Exception:
        pass


def cache_prune():
    try:
        con = _cache_conn()
        rows_before = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        
        now = dt.datetime.now().astimezone()
        cutoff = (now - dt.timedelta(days=CACHE_MAX_AGE_DAYS)).isoformat(timespec="seconds")
        con.execute("DELETE FROM cache WHERE created < ?", (cutoff,))
        
        con.execute("DELETE FROM cache WHERE key IN ("
                    "SELECT key FROM cache ORDER BY created DESC LIMIT -1 OFFSET ?"
                    ")", (CACHE_MAX_ROWS,))
        
        con.commit()
        rows_after = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        con.close()
        return rows_before, rows_after
    except Exception:
        return -1, -1


def _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                  cache, cost, dt_s, cached=False, via=None, cache_miss=None):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_asked": model, "model_echoed": echoed, "id": rid,
        "session": session or None, "project": project, "commit": commit,
        "in": pin, "out": pout, "cache": cache,
        "cost_usd": round(cost, 6), "latency_s": round(dt_s, 2),
        "cached": cached,
    }
    # For standalone delegate, use MODELS quota channel if not explicitly provided
    q_channel = MODELS.get(model, {}).get("quota_channel") if model in MODELS else None
    if q_channel:
        rec["quota_channel"] = q_channel
        
    if via is not None:
        rec["via"] = via
    if cache_miss is not None:
        rec["cache_miss"] = cache_miss
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


# ---- provider calls ----------------------------------------------------------
def call_openai(spec, key, history, system, max_output_tokens: int = 8192):
    msgs = ([{"role": "system", "content": system}] if system else []) + history
    r = _post_with_retry(spec["api"], f"{spec['url']}/chat/completions",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"model": spec["api"], "messages": msgs, "max_tokens": max_output_tokens})
    d = r.json()
    try:
        content = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise ProviderError(spec["api"], 200, "malformed response: missing choices[0].message.content")
    u = d.get("usage", {})
    
    # Priority: DeepSeek explicit prompt_cache_hit_tokens, fallback to OpenAI compat field
    cache_hit = u.get("prompt_cache_hit_tokens")
    if cache_hit is not None:
        cache = cache_hit
        cache_miss = u.get("prompt_cache_miss_tokens")
    else:
        cache = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        cache_miss = None

    return (content, d.get("model"), d.get("id"),
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
            cache, cache_miss)


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
    # Key travels in the x-goog-api-key header, never in the URL: URLs end up
    # in exception messages, logs and tracebacks (real leak 2026-07-15).
    r = _post_with_retry(spec["api"], f"{spec['url']}/models/{spec['api']}:generateContent",
                         headers={"Content-Type": "application/json", "x-goog-api-key": key}, json=body)
    d = r.json()
    try:
        text = d["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise ProviderError(spec["api"], 200, "malformed response: missing candidates[0].content.parts[0].text")
    um = d.get("usageMetadata", {})
    return (text, d.get("modelVersion", spec["api"]), d.get("responseId"),
            um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0),
            um.get("cachedContentTokenCount", 0), None)


# Sentinel-line protocol (not markdown fences: file content may itself contain
# backticks). Full-file replacement only — cheap models are unreliable with diffs.
# Rationale: Markdown code fences fail when the target file contains fences itself.
# Known limit: A literal `===END FILE===` line inside the target code would truncate
# the parse. This is accepted because worker files are code, so a sentinel collision
# is purely theoretical and extremely unlikely in practice.
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

CONTEXT_DISCIPLINE_PREAMBLE = """=== CONTEXT DISCIPLINE ===
- Read a file ONCE, whole; never re-read an unchanged file (scroll, don't re-fetch).
- Prefer `grep -n` to locate, then read ONLY the needed section.
- Batch related reads into one command, not N small ones.
- One WO phase per session; end session between phases.
- Never paste large file bodies into your own replies/summaries.
- At task end, report tokens/cost if the harness exposes them.
==========================
"""


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
        # shell=True is deliberate and required to support shell pipelines (e.g., cmd1 | cmd2)
        # in verify commands. The caller is trusted by design, so shlex/shell=False is rejected.
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=VERIFY_TIMEOUT)
        ok = r.returncode == 0
        output = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        ok, output = False, f"TIMEOUT after {VERIFY_TIMEOUT}s"
    return ok, output, time.time() - t0


def _get_channel_system_prompt(model: str) -> str:
    if model in ("flash", "pro", "deepseek"):
        channel = "deepseek"
    elif model in ("minimax", "m3"):
        channel = "minimax"
    elif model in ("gemini", "gemini-lite", "gemma", "Gemini 3.1 Pro (High)"):
        channel = "gemini"
    else:
        channel = model
        
    try:
        p = Path(__file__).parent.parent / "templates" / "system-prompts" / f"{channel}.md"
        if p.exists():
            return p.read_text().strip() + "\n\n"
    except Exception:
        pass
    return ""


def build_worker_prompt(task: str, file_specs: list, model: str | None = None) -> str:
    import repo_map
    parts = []
    if model:
        channel_prompt = _get_channel_system_prompt(model)
        if channel_prompt:
            parts.append(channel_prompt)
    parts.append(CONTEXT_DISCIPLINE_PREAMBLE)
    parts.append(repo_map.generate_repo_map(cwd="."))
    # Prefix-cache invariant: constant text (preamble, repo map) precedes the
    # files block; the variable task text stays last.
    for path, content in file_specs:
        parts.append(f"===CURRENT FILE: {path}===\n{content}\n===END CURRENT FILE===\n")
    parts.append(f"Task:\n{task}\n")
    return "\n".join(parts)


def _format_worker_summary(written, rejected, verify_cmd, verify_status, attempt,
                            max_attempts, elapsed, summary, total_files, cost,
                            echoed_model, fail_tail, hit_rates):
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
    hr_str = ", ".join(hit_rates) if hit_rates else "0.0%"
    lines.append(f"cost          : ${cost:.6f} · model echoed: {echoed_model} · cache hit rate: {hr_str}")
    if verify_status == "FAIL" and fail_tail:
        lines.append("")
        lines.append("verify output (last 15 lines):")
        lines.append(_tail_lines(fail_tail, 15))
    return "\n".join(lines)


def _worker_delegate_inner(task: str, model: str, files_arg: str, allow_write_arg: str,
                     verify_cmd: str, retries: int, project_root: Path = None,
                     via: str | None = None, estimate: bool = False) -> str:
    """Worker mode per DELEGATE-TOOL-DESIGN.md SPEC v1. Only the returned summary
    (≤25 lines) is meant to reach Claude's context — golden rule."""
    spec = MODELS[model]
    key = os.environ.get(spec["key"], "")
    if not key:
        sys.exit(f"❌ {spec['key']} not set in vault .env")

    project_root = project_root or Path.cwd()
    project, commit = project_info()

    if estimate:
        # Heuristic length of prompt including files
        est_len = len(task)
        for rel in (f.strip() for f in (files_arg or "").split(",") if f.strip()):
            p = project_root / rel
            if p.exists():
                est_len += len(p.read_text())
        prompt_len = est_len // 4
        est_cost = prompt_len / 1e6 * spec["cin"] + 8192 / 1e6 * spec["cout"]
        print(f"ESTIMATE for {model} ({spec['api']}):")
        print(f"  Input tokens : ~{prompt_len} (heuristic)")
        print("  Output tokens: 8192 (assumed max)")
        print(f"  Price/1M     : in=${spec['cin']:.3f} / out=${spec['cout']:.3f}")
        print(f"  Cost USD     : ~${est_cost:.6f}")
        check_budget(project, None, print_estimate=True, model_spec=spec)

    check_budget(project, None, model_spec=spec)

    rel_files = [f.strip() for f in files_arg.split(",") if f.strip()] if files_arg else []
    allow_patterns = [p.strip() for p in allow_write_arg.split(",") if p.strip()] if allow_write_arg else []
    max_attempts = min(max(retries, 0), 2) + 1

    file_specs = []
    for rel in rel_files:
        p = project_root / rel
        content = p.read_text() if p.exists() else "(file does not exist yet)"
        file_specs.append((rel, content))

    caller = call_gemini if spec["provider"] == "gemini" else call_openai
    # Prefix discipline: system prompt (WORKER_PROTOCOL_SYSTEM) is the constant head;
    # history is append-only for retries; files come before the task string.
    history = [{"role": "user", "content": build_worker_prompt(task, file_specs, model)}]
    total_cost = 0.0
    echoed_model = spec["api"]
    hit_rates = []

    def call_once():
        nonlocal total_cost, echoed_model
        answer, echoed, rid, pin, pout, cache, cache_miss = caller(spec, key, history, WORKER_PROTOCOL_SYSTEM)
        
        cached = min(cache, pin)
        cin = spec["cin"]
        cin_cached = spec.get("cin_cached", cin)
        total_cost += (pin - cached) / 1e6 * cin + cached / 1e6 * cin_cached + pout / 1e6 * spec["cout"]
        
        echoed_model = echoed or echoed_model
        if pin > 0:
            hit_rates.append(f"{cache/pin*100:.1f}%")
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
                                  echoed_model, fail_output, hit_rates)


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
    # For standalone worker delegate, use MODELS quota channel if not explicitly provided
    q_channel = MODELS.get(model, {}).get("quota_channel") if model in MODELS else None
    if q_channel:
        rec["quota_channel"] = q_channel
        
    if via is not None:
        rec["via"] = via
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def worker_delegate(task: str, model: str, files_arg: str, allow_write_arg: str,
                     verify_cmd: str, retries: int, project_root: Path = None,
                     via: str | None = None, estimate: bool = False) -> str:
    ch = get_model_channel(model)
    if not is_channel_enabled(ch):
        msg = f"channel {ch} disabled in channels.json"
        print(msg)
        logger.warning(msg)
        if model in ("minimax", "gemini"):
            return worker_delegate(task, "flash", files_arg, allow_write_arg, verify_cmd, retries, project_root, via, estimate)
        raise ValueError(f"All candidates disabled (last tried: {ch})")

    try:
        return _worker_delegate_inner(task, model, files_arg, allow_write_arg, verify_cmd, retries, project_root, via, estimate)
    except ProviderError as e:
        if model == "minimax" and e.status in (401, 402, 429):
            logger.warning(f"⚠️  MiniMax failed (HTTP {e.status}) — credit likely exhausted. "
                  f"Per policy MiniMax is NOT recharged; falling back to DeepSeek flash. "
                  f"Make 'flash' your default from now on.")
            return worker_delegate(task, "flash", files_arg, allow_write_arg, verify_cmd, retries, project_root, via, estimate)
        if model == "gemini" and e.status == 429:
            logger.warning("⚠️  Gemini free tier exhausted (HTTP 429). Falling back to DeepSeek flash. "
                  "Spend is now NONZERO.")
            return worker_delegate(task, "flash", files_arg, allow_write_arg, verify_cmd, retries, project_root, via, estimate)
        raise

def _delegate_inner(prompt: str, model: str, session: str = "", system: str = "",
             use_cache: bool = True, max_output_tokens: int = 8192,
             via: str | None = None, estimate: bool = False) -> str:
    spec = MODELS[model]
    key = os.environ.get(spec["key"], "")
    if not key:
        sys.exit(f"❌ {spec['key']} not set in vault .env")

    project, commit = project_info()

    if estimate:
        prompt_len = len(prompt + system) // 4
        est_cost = prompt_len / 1e6 * spec["cin"] + max_output_tokens / 1e6 * spec["cout"]
        print(f"ESTIMATE for {model} ({spec['api']}):")
        print(f"  Input tokens : ~{prompt_len} (heuristic)")
        print(f"  Output tokens: {max_output_tokens} (assumed max)")
        print(f"  Price/1M     : in=${spec['cin']:.3f} / out=${spec['cout']:.3f}")
        print(f"  Cost USD     : ~${est_cost:.6f}")
        check_budget(project, session, print_estimate=True, model_spec=spec)

    check_budget(project, session, model_spec=spec)

    # Exact-hash cache: only for stateless one-shots (a --session call is a
    # multi-turn conversation, never safe to serve from a single cached turn).
    cache_key = cache_make_key(model, system, prompt, max_output_tokens) if (use_cache and not session) else None
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
    logger.debug(f"key: set (len={len(key)})")

    t0 = time.time()
    caller = call_gemini if spec["provider"] == "gemini" else call_openai
    answer, echoed, rid, pin, pout, cache, cache_miss = caller(
        spec, key, history, system, max_output_tokens=max_output_tokens)
    
    dt_s = time.time() - t0
    
    cached = min(cache, pin)
    cin = spec["cin"]
    cin_cached = spec.get("cin_cached", cin)
    cost = (pin - cached) / 1e6 * cin + cached / 1e6 * cin_cached + pout / 1e6 * spec["cout"]

    print(format_proof(echoed, rid, pin, pout, cache, cost, dt_s, spec["cin"] == 0))

    if session:
        history.append({"role": "assistant", "content": answer})
        save_history(session, history)

    if cache_key:
        cache_put(cache_key, model, prompt, answer)

    _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                 cache, cost, dt_s, cached=False, via=via, cache_miss=cache_miss)
    return answer


def format_proof(echoed: str, rid: str, pin: int, pout: int, cache: int, cost: float, dt_s: float, is_free: bool) -> str:
    hit_rate = f" ({cache/pin*100:.1f}%)" if pin > 0 else ""
    free_str = "  (FREE tier)" if is_free else ""
    return (
        "\n===== PROOF (from provider's server) =====\n"
        f"model echoed : {echoed}\n"
        f"response id  : {rid}\n"
        f"usage        : in={pin} out={pout} cache={cache}{hit_rate}\n"
        f"cost         : ${cost:.6f}{free_str}\n"
        f"latency      : {dt_s:.2f}s\n"
        "==========================================\n"
    )


def get_last_cost() -> float:
    """Read the cost_usd of the audit line delegate() just wrote. Synchronous,
    single-call-at-a-time server: the last line is always ours."""
    if not AUDIT.exists():
        return 0.0
    lines = AUDIT.read_text().strip().splitlines()
    if not lines:
        return 0.0
    return json.loads(lines[-1]).get("cost_usd", 0.0)


def delegate(prompt: str, model: str, session: str = "", system: str = "",
             use_cache: bool = True, max_output_tokens: int = 8192,
             via: str | None = None, estimate: bool = False) -> str:
    ch = get_model_channel(model)
    if not is_channel_enabled(ch):
        msg = f"channel {ch} disabled in channels.json"
        print(msg)
        logger.warning(msg)
        if model in ("minimax", "gemini"):
            return delegate(prompt, "flash", session, system, use_cache, max_output_tokens, via, estimate)
        raise ValueError(f"All candidates disabled (last tried: {ch})")

    try:
        return _delegate_inner(prompt, model, session, system, use_cache, max_output_tokens, via, estimate)
    except ProviderError as e:
        if model == "minimax" and e.status in (401, 402, 429):
            logger.warning(f"⚠️  MiniMax failed (HTTP {e.status}) — credit likely exhausted. "
                  f"Per policy MiniMax is NOT recharged; falling back to DeepSeek flash. "
                  f"Make 'flash' your default from now on.")
            return delegate(prompt, "flash", session, system, use_cache, max_output_tokens, via, estimate)
        if model == "gemini" and e.status == 429:
            logger.warning("⚠️  Gemini free tier exhausted (HTTP 429). Falling back to DeepSeek flash. "
                  "Spend is now NONZERO.")
            return delegate(prompt, "flash", session, system, use_cache, max_output_tokens, via, estimate)
        raise

def _write_agent_audit(model, echoed, project, commit, files_changed_count, verify_status, cost_usd, cost_unknown, quota_channel, via=None, runner=None, exit_code=None, run_id=None, premium_requests=None):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_asked": model, "model_echoed": echoed,
        "session": None, "project": project, "commit": commit,
        "cost_usd": round(cost_usd, 6), "cached": False,
        "mode": "agent",
        "runner": runner,
        "files_changed_count": files_changed_count,
        "verify_status": verify_status,
        "quota_channel": quota_channel,
    }
    if premium_requests is not None:
        rec["premium_requests"] = premium_requests
    if exit_code not in (0, None):
        rec["runner_exit"] = exit_code
    if run_id:
        rec["run_id"] = run_id
    if cost_unknown:
        rec["cost_unknown"] = True
    if via is not None:
        rec["via"] = via
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def agent_delegate(task: str, runner: str = "agy", model: str | None = None, workdir: str | Path | None = None, verify_cmd: str = "", via: str | None = None, estimate: bool = False, timeout_s: int = 600) -> str:
    import signal
    import tempfile
    import repo_map
    
    project_root = Path(workdir) if workdir else Path.cwd()
    project, commit = project_info()

    if not is_channel_enabled(runner):
        msg = f"channel {runner} disabled in channels.json"
        print(msg)
        logger.warning(msg)
        raise ValueError(f"All candidates disabled (last tried: {runner})")

    if model and "claude" in model.lower() and runner != "copilot":
        # The ban exists because Claude via the Anthropic sub double-bills; the
        # copilot runner bills Claude as Copilot premium requests instead.
        raise ValueError("Claude models are banned inside delegate (subscription-billed; routing them here double-bills)")

    if runner == "agy":
        model_name = model or "Gemini 3.1 Pro (High)"
        quota_channel = "google-ai-pro"
        spec = {"api": model_name, "quota_channel": quota_channel, "cin": 0, "cout": 0}
    elif runner == "codewhale":
        model_name = model or "flash"
        if model_name not in ["flash", "minimax"]:
            if model_name in ALIASES and ALIASES[model_name] in ["flash", "minimax"]:
                model_name = ALIASES[model_name]
            else:
                raise ValueError("codewhale runner only supports 'flash' or 'minimax' models")
        
        provider_model = MODELS[model_name]
        quota_channel = provider_model.get("quota_channel", f"{model_name}-api")
        spec = provider_model
    elif runner == "codex":
        model_name = model or "gpt-5.1-codex"
        quota_channel = "chatgpt-sub"
        spec = {"api": model_name, "quota_channel": quota_channel, "cin": 0, "cout": 0}
    elif runner == "copilot":
        model_name = model or "claude-sonnet-4.5"
        quota_channel = "copilot-sub"
        spec = {"api": model_name, "quota_channel": quota_channel, "cin": 0, "cout": 0}
    else:
        raise ValueError("runner must be 'agy', 'codewhale', 'codex', or 'copilot'")

    channel_prompt = _get_channel_system_prompt(model_name)
    task = f"{channel_prompt}{CONTEXT_DISCIPLINE_PREAMBLE}\n{repo_map.generate_repo_map(str(project_root))}\nTask:\n{task}"

    if estimate:
        print(f"ESTIMATE for {runner} ({model_name}):")
        print(f"  Quota channel: {quota_channel}")
        check_budget(project, None, print_estimate=True, model_spec=spec)
        return "estimate only"

    check_budget(project, None, model_spec=spec)

    def _git_status():
        try:
            return subprocess.run(["git", "-C", str(project_root), "status", "--porcelain"], capture_output=True, text=True, timeout=GIT_TIMEOUT).stdout.strip()
        except Exception:
            return ""

    status_before = _git_status()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stdout_fd, stdout_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix="agent_", suffix=".log")
    os.close(stdout_fd)

    run_env = None
    if runner == "agy":
        # agy print mode kills any run whose next response exceeds
        # --print-timeout (default 5m) — size it to our own timeout.
        # --dangerously-skip-permissions: since agy 1.1.3 (2026-07-16),
        # accept-edits no longer auto-approves write_file/command in print
        # mode — every headless run died in 18-41s with "permission check
        # failed ... auto-denied". agy has no settings file for scoped
        # allow-rules (verified: no ~/.agy or ~/.config/agy), so the
        # documented skip flag is the only headless path; router-managed
        # launches only, never interactive sessions.
        cmd = ["agy", "-p", task, "--model", model_name, "--mode", "accept-edits",
               "--dangerously-skip-permissions",
               "--print-timeout", f"{timeout_s}s"]
    elif runner == "codewhale":
        # Flags verified against `codewhale exec --auto --help` (2026-07-14):
        # plain exec is a one-shot text reply; --auto enables tool-backed agent
        # mode, --json emits a machine-readable summary, --model overrides the
        # model per run, --max-turns caps model steps. Confinement = Popen cwd
        # + global -C/--workspace; exec exposes no sandbox-mode/approval flags.
        # Live-verified 2026-07-14: --model must sit BEFORE `exec` (the CLI
        # errors otherwise, despite exec --help listing it as forwarded), and
        # the provider is chosen via CODEWHALE_PROVIDER (per `auth status`) —
        # otherwise the model name is sent to whatever provider is active.
        cw_model = "deepseek-v4-flash" if model_name == "flash" else "minimax-m3"
        run_env = {**os.environ, "CODEWHALE_PROVIDER": "deepseek" if model_name == "flash" else "minimax"}
        cmd = ["codewhale", "-C", str(project_root), "--model", cw_model,
               "exec", "--auto", "--json", "--max-turns", "50", task]
    elif runner == "codex":
        # Workdir flag is -C/--cd per openai/codex docs (grok-verified
        # 2026-07-18); binary not installed here, so this runner is
        # DELIVERED-UNSMOKED until a live `codex exec --help` confirms it.
        cmd = ["codex", "exec", "--cd", str(project_root), task]
    elif runner == "copilot":
        cmd = ["copilot", "-p", task, "--allow-all-tools", "--model", model_name]
        
    t0 = time.time()
    timed_out = False
    exit_code = None

    with open(stdout_path, "w") as f:
        try:
            proc = subprocess.Popen(cmd, cwd=project_root, stdout=f, stderr=subprocess.STDOUT, preexec_fn=os.setsid, env=run_env)
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait()
            timed_out = True
        except FileNotFoundError:
            sys.exit(f"❌ {runner} binary not found in PATH")

    elapsed = time.time() - t0
    
    status_after = _git_status()
    files_changed = []
    before_lines = set(status_before.splitlines())
    after_lines = set(status_after.splitlines())
    for line in (after_lines - before_lines):
        files_changed.append(line)

    cost_usd = 0.0
    cost_unknown = False
    run_id = None
    if runner == "codewhale":
        cost_unknown = True
        # First choice: the exec --json summary we captured. Live-verified
        # 2026-07-14: it's a pretty-printed JSON object preceded by terminal
        # escape junk, carrying status/tools but (today) no cost or session
        # id — parsed defensively anyway so future fields activate; on any
        # surprise we fall through, never invent numbers.
        try:
            content = Path(stdout_path).read_text()
            start = content.find("{")
            if start != -1:
                data = json.JSONDecoder().raw_decode(content[start:])[0]
                run_id = data.get("session_id") or data.get("sessionId")
                for key in ("cost_usd", "total_cost_usd", "cost"):
                    if isinstance(data.get(key), (int, float)):
                        cost_usd = float(data[key])
                        cost_unknown = False
                        break
        except Exception:
            pass
        if cost_unknown:
            # Fallback: audit-log rollup, window sized to this run (metrics
            # --since takes durations like 30m; 1m would miss a long run).
            since = f"{int(elapsed // 60) + 2}m"
            try:
                r = subprocess.run(["codewhale", "metrics", "--json", "--since", since], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    data = json.loads(r.stdout)
                    if "cost_usd" in data:
                        cost_usd = float(data["cost_usd"])
                        cost_unknown = False
            except Exception:
                pass

    verify_status = "SKIPPED"
    verify_elapsed = 0.0
    fail_output = ""
    if verify_cmd and not timed_out:
        ok, vout, verify_elapsed = run_verify(verify_cmd, project_root)
        verify_status = "PASS" if ok else "FAIL"
        if not ok:
            fail_output = vout
            
    premium_req = 1 if runner == "copilot" else None
            
    _write_agent_audit(model_name, model_name, project, commit, len(files_changed), verify_status, cost_usd, cost_unknown, quota_channel, via=via, runner=runner, exit_code=exit_code, run_id=run_id, premium_requests=premium_req)

    if timed_out:
        status = "TIMEOUT — process group killed"
    elif exit_code != 0:
        status = f"FAILED (exit {exit_code})"
    else:
        status = "COMPLETED"
    lines = [
        f"runner        : {runner} ({model_name})",
        f"status        : {status} ({elapsed:.1f}s)",
        f"files changed : {len(files_changed)} files",
    ]
    if run_id:
        lines.append(f"resume        : codewhale exec --resume {run_id}")
    if files_changed:
        lines.append(f"changes       : {', '.join([c.split()[-1] for c in files_changed[:3]])}" + ("..." if len(files_changed) > 3 else ""))
        
    if verify_cmd:
        lines.append(f"verify        : {verify_cmd} → {verify_status}" + (f" ({verify_elapsed:.1f}s)" if verify_status != "SKIPPED" else ""))
    else:
        lines.append("verify        : (skipped — no --verify given)")
        
    if runner == "codewhale":
        lines.append(f"cost          : {'unknown — see codewhale audit' if cost_unknown else f'${cost_usd:.6f}'}")
    
    lines.append(f"output saved  : {stdout_path}")
    
    if verify_status == "FAIL" and fail_output:
        lines.append("")
        lines.append("verify output (last 10 lines):")
        lines.append(_tail_lines(fail_output, 10))
        
    return "\n".join(lines[:25])


def cmd_channels(enable_channel=None, disable_channel=None):
    import shutil
    channels_json = DATA_DIR / "channels.json"
    
    if enable_channel or disable_channel:
        data = {}
        if channels_json.exists():
            try:
                import json
                data = json.loads(channels_json.read_text())
            except Exception:
                pass
        if enable_channel:
            if enable_channel not in data:
                data[enable_channel] = {}
            data[enable_channel]["enabled"] = True
            print(f"Enabled channel: {enable_channel}")
        if disable_channel:
            if disable_channel not in data:
                data[disable_channel] = {}
            data[disable_channel]["enabled"] = False
            print(f"Disabled channel: {disable_channel}")
        channels_json.write_text(json.dumps(data, indent=2))
        return

    print(f"{'CHANNEL':<15} | {'ENABLED':<8} | {'BIN/PATH':<25} | {'AUTH/NOTES'}")
    print("-" * 75)
    
    data = {}
    if channels_json.exists():
        try:
            import json
            data = json.loads(channels_json.read_text())
        except Exception:
            pass
            
    env_disabled = [c.strip() for c in os.environ.get("AI_ROUTER_DISABLE_CHANNELS", "").split(",") if c.strip()]
    
    for ch in ["agy", "codex", "copilot", "codewhale", "gemini-free", "deepseek", "minimax", "grok"]:
        enabled = data.get(ch, {}).get("enabled", True)
        if ch in env_disabled:
            enabled = False
        
        enabled_str = "yes" if enabled else "NO"
        
        bin_str = "-"
        auth_str = data.get(ch, {}).get("notes", "")
        
        if ch in ("agy", "codewhale", "codex", "copilot"):
            bin_path = shutil.which(ch)
            if bin_path:
                bin_str = bin_path
                
                if ch == "codex":
                    try:
                        r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=2)
                        if r.returncode == 0:
                            auth_str = r.stdout.strip().split("\n")[0]
                    except Exception:
                        pass
                elif ch == "copilot":
                    # Copilot CLI relies on GH CLI or env vars, no native auth status command
                    auth_str = "assumed via env or GH CLI"
            else:
                bin_str = "missing"
                
        print(f"{ch:<15} | {enabled_str:<8} | {bin_str:<25} | {auth_str}")


def main():
    ap = argparse.ArgumentParser(description="Delegate a task to a grunt/free model, with proof + memory.")
    ap.add_argument("-q", "--quiet", action="store_true", help="suppress INFO logs")
    ap.add_argument("-p", "--prompt")
    ap.add_argument("--plan", help="read the prompt from this file")
    ap.add_argument("--out", help="write the answer to this file (else stdout)")
    ap.add_argument("--model", default=None,
                    help="model or alias (minimax|flash|pro|grok|gemini or full names); "
                         "chat/worker default: minimax; agent mode: per-runner default")
    ap.add_argument("--session", default="", help="conversation name to remember across calls")
    ap.add_argument("--new", action="store_true", help="reset the named session before running")
    ap.add_argument("--system", default="", help="system instruction (persona / rules)")
    ap.add_argument("--audit", action="store_true", help="print the delegation ledger and exit")
    ap.add_argument("--cost", action="store_true", help="print the cost report and exit")
    ap.add_argument("--channels", action="store_true", help="list channel registry status")
    ap.add_argument("--enable", help="enable a channel in channels.json")
    ap.add_argument("--disable", help="disable a channel in channels.json")
    ap.add_argument("--cache-prune", action="store_true", help="prune old/excess cache rows and exit")
    ap.add_argument("--estimate", action="store_true", help="print estimated cost and caps, without calling the provider")
    ap.add_argument("--since", help="YYYY-MM-DD to filter cost report")
    ap.add_argument("--today", action="store_true", help="shortcut for --since today")
    ap.add_argument("--by", default="model", help="group cost report by (model|project|session|via|day)")
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
    ap.add_argument("--agent", action="store_true", help="agent mode: use agy or codewhale exec for multi-step exploration")
    ap.add_argument("--runner", default="agy", help="agent mode runner: agy (default) or codewhale")
    ap.add_argument("--timeout", type=int, default=600, help="agent mode: timeout in seconds (default 600, max 1800)")
    a = ap.parse_args()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING if a.quiet else logging.DEBUG)

    if a.audit:
        show_audit()
        return
    if a.cost:
        since = dt.datetime.now().astimezone().isoformat()[:10] if a.today else a.since
        show_cost(since=since, by=a.by)
        return
    if a.cache_prune:
        before, after = cache_prune()
        if before == -1:
            sys.exit("❌ Cache prune failed (DB error/missing)")
        print(f"🧹 Cache prune: {before} -> {after} rows (-{before - after})")
        return
    if a.new and a.session:
        f = SESSIONS / f"{a.session}.json"
        f.exists() and f.unlink()
        print(f"↺ session '{a.session}' reset")
        if not (a.prompt or a.plan):
            return

    if a.channels or a.enable or a.disable:
        cmd_channels(enable_channel=a.enable, disable_channel=a.disable)
        return

    load_env()
    prompt = Path(a.plan).read_text() if a.plan else a.prompt
    if not prompt:
        sys.exit("❌ need -p PROMPT or --plan FILE (or --audit / --new / --channels)")

    if a.agent:
        timeout = min(max(a.timeout, 1), 1800)
        # Raw a.model (None when unset): each runner has its own default, and
        # a resolved chat default like "minimax" is meaningless to agy.
        try:
            print(agent_delegate(prompt, runner=a.runner, model=a.model, workdir=Path.cwd(), verify_cmd=a.verify, estimate=a.estimate, timeout_s=timeout))
        except ValueError as e:
            sys.exit(f"❌ {e}")
        return

    try:
        model = resolve_model(a.model or "minimax")
    except ValueError as e:
        sys.exit(f"❌ {e}")

    if a.files:
        print(worker_delegate(prompt, model, a.files, a.allow_write, a.verify, a.retries, estimate=a.estimate))
        return

    use_cache = not a.no_cache
    answer = delegate(prompt, model, a.session, a.system, use_cache=use_cache, estimate=a.estimate)

    if a.out:
        Path(a.out).write_text(answer)
        print(f"answer written → {a.out} ({len(answer)} chars)")
    else:
        print(answer)


if __name__ == "__main__":
    main()
