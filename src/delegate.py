#!/usr/bin/env python3
"""delegate.py — single LLM gateway for grunt-work, with PROOF + audit + memory.

Every call prints the model name ECHOED BY THE PROVIDER'S SERVER (not by us), the
response id, token usage and computed cost — then appends one line to audit.log so
you have an independent ledger. Optional conversation memory (--session) makes coding
iterative ("now add tests") instead of one-shot.

Providers:
  agy     — Gemini 3.1 Pro via the `agy` CLI, Google AI Pro subscription ($0)
  minimax — MiniMax-M3 (prepaid, spend first)
  flash   — deepseek-v4-flash    pro — deepseek-v4-pro    grok — grok-4.3

Note: `gemini`/`gemini-lite`/`gemma` (the free-quota Gemini API channel) were
REMOVED 2026-07-27 — they silently overrode the $0 `agy` default and 429'd
into paid fallback constantly. See REMOVED_MODELS below; resolve_model()
raises loudly for these names instead of silently remapping them.

Usage:
  python3 delegate.py -p "prompt"                       # default model = minimax
  python3 delegate.py --model deepseek-v4-flash --plan PLAN.md --out ANSWER.md
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
"""  # noqa: EXE001
import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import httpx

HTTP_TIMEOUT = 180
VERIFY_TIMEOUT = 600
GIT_TIMEOUT = 3
SQLITE_TIMEOUT = 5
CACHE_MAX_ROWS = 5000
CACHE_MAX_AGE_DAYS = 90
# xAI /v1/responses: default cap on server-side web_search calls per request.
# A live A/B (2026-07-27) found an uncapped grok-4.5 question made 15 calls,
# pulled 209,956 input tokens, and cost $0.389 in a single call (at
# $0.005/call plus the search-result tokens, an unbounded question can cost
# 100x what the caller expects). Overridable via --max-tool-calls / MCP arg.
XAI_MAX_TOOL_CALLS = 6

logger = logging.getLogger("ai_router")

_KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s'\"]+")

def _redact(text: str) -> str:
    """Scrub secrets from any text, masking the key (showing last 4 chars)."""
    if not isinstance(text, str):
        return text
    
    def mask_param(match):
        prefix = match.group(1)
        val = match.group(0)[len(prefix):]
        if len(val) > 4:
            return f"{prefix}<redacted...{val[-4:]}>"
        return f"{prefix}<redacted>"
        
    text = _KEY_PARAM_RE.sub(mask_param, text)
    
    for spec in MODELS.values():
        env_name = spec.get("key")
        if env_name:
            value = os.environ.get(env_name)
            if value:
                if len(value) > 4:
                    text = text.replace(value, f"<{env_name}...{value[-4:]}>")
                else:
                    text = text.replace(value, f"<{env_name}>")
    return text

class RedactingFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(str(arg)) if isinstance(arg, str) else arg for arg in record.args)
        return True

class ProviderError(Exception):
    def __init__(self, model, status, short_reason):
        short_reason = _redact(short_reason)
        super().__init__(f"{model} failed: HTTP {status} ({short_reason})")
        self.model = model
        self.status = status
        self.short_reason = short_reason

# xAI's /v1/responses endpoint reports its OWN billed cost in
# usage.cost_in_usd_ticks (1 tick = 1e-10 USD) — this is ground truth
# (WO-ai-router-0024, live-verified 2026-07-27) and MUST override the
# token-table estimate, because each server-side web_search call bills a
# flat $0.005 that no token count can see. call_xai_responses stashes it
# here (module-level, not a return value) so the existing 7-tuple
# provider-call contract every caller relies on stays unchanged; the caller
# pops it right after invoking the provider function.
_LAST_TRUE_COST: dict | None = None


def _pop_last_true_cost() -> dict | None:
    global _LAST_TRUE_COST
    val = _LAST_TRUE_COST
    _LAST_TRUE_COST = None
    return val


def compute_token_cost(spec: dict, pin: int, pout: int, cached: int) -> float:
    """Table-based fallback cost — used whenever a provider does not report
    a true billed cost (every provider except xAI's /v1/responses). Honors
    cin_cached for the cached slice of pin, and the long-context surcharge
    some providers apply once pin exceeds long_ctx_threshold input tokens
    (cin_long/cout_long) — unmodelled before WO-ai-router-0024, which
    under-reported cost on any call whose prompt exceeded that threshold.
    """
    cached = min(cached, pin)
    threshold = spec.get("long_ctx_threshold")
    if threshold is not None and pin > threshold:
        cin, cout = spec.get("cin_long", spec["cin"]), spec.get("cout_long", spec["cout"])
    else:
        cin, cout = spec["cin"], spec["cout"]
    cin_cached = spec.get("cin_cached", cin)
    return (pin - cached) / 1e6 * cin + cached / 1e6 * cin_cached + pout / 1e6 * cout


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
            reason = f"{type(e).__name__}: {e!s}"

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
    "minimax": {"api": "MiniMax-M3",        "provider": "openai", "url": "https://api.minimax.io/v1",
                    "cin": 0.30, "cin_cached": 0.06, "cout": 1.20, "key": "MINIMAX_API_KEY", "quota_channel": "minimax-api"},
    "flash":   {"api": "deepseek-v4-flash", "provider": "openai", "url": "https://api.deepseek.com/v1",
                    "cin": 0.14, "cin_cached": 0.014, "cout": 0.28, "key": "DEEPSEEK_API_KEY", "quota_channel": "deepseek-api"},
    "pro":     {"api": "deepseek-v4-pro",   "provider": "openai", "url": "https://api.deepseek.com/v1",
                    "cin": 0.435, "cin_cached": 0.0435, "cout": 0.87, "key": "DEEPSEEK_API_KEY", "quota_channel": "deepseek-api"},
    "grok":    {"api": "grok-4.3",          "provider": "xai", "url": "https://api.x.ai/v1",
                    "cin": 1.25, "cin_cached": 0.20, "cout": 2.50,
                    "cin_long": 2.50, "cout_long": 5.00, "long_ctx_threshold": 200_000,
                    "search_call_usd": 0.005,
                    "key": "GROK_API_KEY", "quota_channel": "grok-api"},
    "grok-4.5": {"api": "grok-4.5",         "provider": "xai", "url": "https://api.x.ai/v1",
                    "cin": 2.00, "cin_cached": 0.30, "cout": 6.00,
                    "cin_long": 4.00, "cout_long": 12.00, "long_ctx_threshold": 200_000,
                    "search_call_usd": 0.005,
                    "key": "GROK_API_KEY", "quota_channel": "grok-api"},
    # $0 coding default (owner decree 2026-07-27). provider "agy_cli" is NOT an
    # HTTP provider — it is dispatched to call_agy_print() (subprocess to the
    # `agy` binary), never to call_openai/call_gemini. "key": "" is deliberate:
    # agy authenticates via its own CLI session (Google AI Pro subscription),
    # not an env-var API key — see the provider=="agy_cli" special case in
    # _worker_delegate_inner.
    # `api` is the model id EXACTLY as `agy models` prints it, and `effort` is
    # always None. Verified empirically 2026-07-29 against the live CLI: the
    # effort level is already baked into the model id (`-high`/`-medium`/`-low`),
    # and passing it separately is not merely redundant — for the Claude models
    # agy hard-errors with `--effort is not supported for model "..."`. One
    # uniform rule (full id, no --effort) is what actually works for all 11.
    "gemini-3.6-flash-high": {"api": "gemini-3.6-flash-high", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.6-flash-medium": {"api": "gemini-3.6-flash-medium", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.6-flash-low": {"api": "gemini-3.6-flash-low", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.5-flash-high": {"api": "gemini-3.5-flash-high", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.5-flash-medium": {"api": "gemini-3.5-flash-medium", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.5-flash-low": {"api": "gemini-3.5-flash-low", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.1-pro-high": {"api": "gemini-3.1-pro-high", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "gemini-3.1-pro-low": {"api": "gemini-3.1-pro-low", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gemini"},
    "claude-sonnet-4-6": {"api": "claude-sonnet-4-6", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-claude"},
    "claude-opus-4-6-thinking": {"api": "claude-opus-4-6-thinking", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-claude"},
    "gpt-oss-120b-medium": {"api": "gpt-oss-120b-medium", "effort": None, "provider": "agy_cli", "url": "", "cin": 0.0, "cout": 0.0, "key": "", "quota_channel": "google-ai-pro-gpt"},
}

# Friendly aliases -> canonical key.
ALIASES = {
    "minimax": "minimax", "minimax-m3": "minimax", "m3": "minimax",
    "flash": "flash", "deepseek": "flash", "ds": "flash", "deepseek-v4": "flash",
    "deepseek-flash": "flash", "deepseek-v4-flash": "flash",
    "pro": "pro", "reasoner": "pro", "deepseek-pro": "pro", "deepseek-v4-pro": "pro",
    "grok": "grok", "grok-4.3": "grok", "grok4": "grok",
    "grok-4.5": "grok-4.5", "grok45": "grok-4.5", "grok4.5": "grok-4.5",
    "agy": "gemini-3.1-pro-high", "antigravity": "gemini-3.1-pro-high", "gemini-3-pro": "gemini-3.1-pro-high",
}

# Owner decree 2026-07-27: the free-quota Gemini API channel ("gemini",
# "gemini-lite", "gemma") is REMOVED, not silently remapped. It defeated the
# standing $0-first policy by being delegate_worker's default, 429'd
# constantly under the free quota, and silently auto-fell back to a PAID
# model without the caller asking (see CHANGELOG "Removed"). resolve_model()
# checks this FIRST and raises loudly — a caller must consciously switch to
# the new default ('agy') or an explicit paid model.
REMOVED_MODELS = {
    "gemini": "agy", "gemini-2.5-flash": "agy", "flash-gemini": "agy",
    "gemini-lite": "agy", "gemini-2.5-flash-lite": "agy", "lite": "agy",
    "gemma": "agy", "gemma-4": "agy", "gemma-4-31b-it": "agy", "gemma3": "agy",
}

# Copilot premium-request multipliers are NOT hardcoded: GitHub changes them
# without notice and exposes no API for them (live-checked 2026-07-19: the
# copilot_internal/v2/token exchange 404s for CLI tokens, and the docs table
# moves between pages). The table lives in <data>/copilot_multipliers.json,
# seeded below on first use; unknown models bill at "default" (1x) so a model
# rename can never silently look free. Ground truth per month comes from the
# GitHub billing API in `r cost` (needs gh 'user' scope).
COPILOT_MULTIPLIERS_SEED = {
    "_doc": (
        "Per-model Copilot premium-request multipliers (Pro plan). Edit freely; "
        "unknown models bill at 'default'. Source: docs.github.com Copilot "
        "billing pages (values verified 2026-07-18)."
    ),
    "default": 1,
    "models": {"gpt-5-mini": 0, "gpt-5": 1, "claude-sonnet-4.5": 1},
}


def copilot_premium_multiplier(model_name: str) -> float:
    path = DATA_DIR / "copilot_multipliers.json"
    try:
        if not path.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(COPILOT_MULTIPLIERS_SEED, indent=2) + "\n")
        data = json.loads(path.read_text())
        models = data.get("models", {})
        if model_name in models:
            return float(models[model_name])
        default = float(data.get("default", 1))
        logger.warning(f"⚠️  copilot model '{model_name}' not in {path.name} — billing {default:g}x premium")
        return default
    except Exception:  # noqa: BLE001
        logger.warning(f"⚠️  could not read {path} — billing 1x premium")
        return 1.0



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
    except Exception:  # noqa: BLE001, S110
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
    key = name.strip().lower()
    if key in REMOVED_MODELS:
        suggestion = REMOVED_MODELS[key]
        raise ValueError(
            f"model '{key}' was removed from the router (owner decree "
            f"2026-07-27: free-quota Gemini flash truncates files and "
            f"silently overrode the agy default). Use '{suggestion}'."
        )
    if key in MODELS:
        return key
    resolved = ALIASES.get(key)
    if resolved is None:
        known = sorted(set(MODELS.keys()) | set(ALIASES.keys()))
        raise ValueError(f"unknown model '{name}'. Known: {', '.join(known)}")
    return resolved


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
            r = subprocess.run(["git", *a], cwd=cwd, capture_output=True,  # noqa: PLW1510
                               text=True, timeout=GIT_TIMEOUT)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            return ""
    remote = git("config", "--get", "remote.origin.url")
    project = remote.rstrip("/").split("/")[-1].removesuffix(".git") if remote else ""
    if not project:
        top = git("rev-parse", "--show-toplevel")
        project = os.path.basename(top) if top else os.path.basename(cwd)
    return (project or os.path.basename(cwd), git("rev-parse", "--short", "HEAD") or None)


def show_audit():
    print(AUDIT.read_text().rstrip() if AUDIT.exists() else "(no audit.log yet)")


def check_budget(project: str, session: str, estimate_cost: float = 0.0, print_estimate: bool = False, model_spec: dict | None = None):
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
        except Exception:  # noqa: BLE001
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
                except Exception:  # noqa: BLE001, S112
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


def show_cost(since: str | None = None, by: str = "model"):
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
            except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001, S110
                pass
        print(f"\ntoday's calls per quota channel ({today_str}):")
        for ch in sorted(today_channel_calls):
            cap = caps.get(ch)
            print(f"  {ch}: {today_channel_calls[ch]}" + (f" / {cap}" if cap is not None else " / (uncapped)"))

    if malformed > 0:
        print(f"\nskipped {malformed} malformed lines")
        
    if copilot_premium_month > 0:
        print(f"\nCopilot premium requests (this month): {copilot_premium_month:g} (ledger estimate; multipliers from copilot_multipliers.json)")
        billed = _github_copilot_billed_this_month()
        if billed is None:
            print("  GitHub-billed Copilot overage: unavailable (run: gh auth refresh -h github.com -s user)")
        elif billed <= 0:
            print("  GitHub-billed Copilot overage: $0.00 (still inside the monthly quota — the API itemizes overage only)")
        else:
            print(f"  GitHub-billed Copilot overage this month: ${billed:.2f} ⚠️  premium quota exceeded — update copilot_multipliers.json if this surprises you")


def _github_copilot_billed_this_month():
    """Net USD GitHub actually billed for Copilot this month, or None.

    There is no public API for premium-request *counts* or multipliers on a
    personal plan (live-checked 2026-07-19: seat_info / copilot usage are
    org-only and 404 without an org; the internal token exchange rejects CLI
    tokens). The billing-usage endpoint only itemizes *overage* — within the
    monthly premium quota it nets to $0. So a non-zero value here means the
    quota was exceeded and real money is being spent: the independent signal
    worth surfacing next to the ledger estimate. Needs gh with the 'user'
    scope; returns None (never raises) when unavailable.
    """
    try:
        login = subprocess.run(["gh", "api", "user", "--jq", ".login"],  # noqa: PLW1510
                               capture_output=True, text=True, timeout=10)
        if login.returncode != 0 or not login.stdout.strip():
            return None
        now = dt.datetime.now()  # noqa: DTZ005
        r = subprocess.run(  # noqa: PLW1510
            ["gh", "api", f"users/{login.stdout.strip()}/settings/billing/usage?year={now.year}&month={now.month}"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        net = 0.0
        for item in json.loads(r.stdout).get("usageItems", []):
            if "copilot" in str(item.get("product", "")).lower():
                net += float(item.get("netAmount", 0) or 0)
        return net
    except Exception:  # noqa: BLE001
        return None


# ---- conversation memory -----------------------------------------------------
def load_history(session: str) -> list:
    f = SESSIONS / f"{session}.json"
    return json.loads(f.read_text()) if f.exists() else []


def save_history(session: str, history: list):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{session}.json").write_text(json.dumps(history, ensure_ascii=False, indent=1))


WORKER_SESSIONS = DATA_DIR / "worker_sessions.json"

def _load_worker_sessions() -> dict:
    if not WORKER_SESSIONS.exists():
        return {}
    try:
        return json.loads(WORKER_SESSIONS.read_text())
    except Exception:
        return {}

def _prune_worker_sessions(sessions: dict) -> dict:
    now = time.time()
    return {k: v for k, v in sessions.items() if now - v.get("ts", 0) <= 86400}

def _save_worker_sessions(sessions: dict):
    pruned = _prune_worker_sessions(sessions)
    WORKER_SESSIONS.parent.mkdir(parents=True, exist_ok=True)
    WORKER_SESSIONS.write_text(json.dumps(pruned, indent=1) + "\n")

def _get_session_conversation(session_key: str) -> str | None:
    return _load_worker_sessions().get(session_key, {}).get("conversation_id")

def _set_session_conversation(session_key: str, conversation_id: str):
    sessions = _load_worker_sessions()
    sessions[session_key] = {"conversation_id": conversation_id, "ts": time.time()}
    _save_worker_sessions(sessions)

def _clear_worker_session(session_key: str | None = None):
    if session_key is None:
        WORKER_SESSIONS.write_text("{}\n")
    else:
        sessions = _load_worker_sessions()
        if session_key in sessions:
            sessions.pop(session_key)
            _save_worker_sessions(sessions)


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
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001, S110
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
    except Exception:  # noqa: BLE001
        return -1, -1


def _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                  cache, cost, dt_s, cached=False, via=None, cache_miss=None,
                  web_search_calls=None):
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
    if web_search_calls is not None:
        rec["web_search_calls"] = web_search_calls
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
    # Deliberately kept even though no MODELS entry registers provider
    # "gemini" today (owner decree 2026-07-27 removed the free-quota
    # gemini/gemini-lite/gemma entries). A future PAID Gemini API model can
    # still register with provider "gemini" and reuse this function — do not
    # "clean up" this plumbing.
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


# agy print-mode timeout for the worker backend. Sized like the CLI's own
# --print-timeout default (5m) — worker tasks are single-file edits, not
# long agentic explorations (that's agent_delegate's job).
AGY_WORKER_TIMEOUT_S = 300

AGY_NO_TOOLS_ADDENDUM = (
    "\n\nIMPORTANT: you are running in headless TEXT-ONLY print mode for "
    "this task. You have no file-editing, shell, or tool-call access from "
    "here — any such attempt is a no-op. Respond with ONLY the sentinel "
    "blocks described above, as plain text. Do not attempt to write or "
    "edit any file yourself.\n"
)


_LAST_AGY_NUM_TURNS: int | None = None
_LAST_AGY_DURATION_S: float | None = None

def call_agy_print(prompt: str, model_name: str, project_root: Path, timeout_s: int = AGY_WORKER_TIMEOUT_S, conversation_id: str | None = None):
    """Invoke `agy` in headless print mode as a pure TEXT GENERATOR for the
    worker protocol (SPEC v1 / PATCH protocol). The router — not agy — is
    the only writer: parse_worker_response() + _write_files()/_apply_patches()
    parse this function's returned text and write to disk themselves.

    Belt-and-braces, TWO independent guards against agy touching the repo:
      1. --mode plan (agy --help confirms `plan` is a real, read-only mode
         alongside `accept-edits`) stops agy from attempting any edit at all.
      2. Deliberately NO --add-dir (verified 2026-07-21, see agent_delegate's
         comment on the same flag for the opposite case): even if plan mode
         were bypassed, without --add-dir agy (antigravity-cli) sandboxes any
         write into ~/.gemini/antigravity-cli/scratch/ instead of
         project_root, so it could never touch the real repo either way.
    The router's own parse_worker_response() + _write_files()/_apply_patches()
    stay the ONLY writer. Do not "fix" this by adding --add-dir or switching
    to --mode accept-edits — that would defeat the whole point of routing
    worker writes through the router instead of trusting the model's tool
    use. Contrast with agent_delegate(), the OPPOSITE case: there agy IS
    meant to be the writer, so it uses --add-dir + --mode accept-edits. Do
    not "harmonise" the two call sites.

    Return shape matches what call_openai/call_gemini return, so
    _worker_delegate_inner can treat all three callers identically:
    (content, echoed_model, request_id, pin, pout, cache, cache_miss).
    Using --output-format json DOES expose real input_tokens/output_tokens/cache_read_tokens,
    and the 3rd tuple slot now carries agy's conversation_id.
    """
    global _LAST_AGY_NUM_TURNS, _LAST_AGY_DURATION_S
    _LAST_AGY_NUM_TURNS = None
    _LAST_AGY_DURATION_S = None

    cmd = ["agy", "-p", prompt, "--model", model_name, "--mode", "plan",
           "--dangerously-skip-permissions", "--output-format", "json",
           "--print-timeout", f"{timeout_s}s"]
    # No --effort, deliberately. The effort level is already baked into the
    # model id (`-high`/`-medium`/`-low`), and agy hard-errors when the flag is
    # sent alongside the Claude ids: `--effort is not supported for model
    # "claude-sonnet-4-6"`. Verified against the live CLI 2026-07-29 — passing
    # the full id with no --effort is the one rule that works for all 11 models.
    if conversation_id is not None:
        cmd.extend(["--conversation", conversation_id])
    try:
        r = subprocess.run(cmd, cwd=str(project_root), capture_output=True,  # noqa: PLW1510
                           text=True, timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        raise ProviderError("agy", "TIMEOUT", f"print mode exceeded {timeout_s}s") from None
    except FileNotFoundError:
        raise ProviderError("agy", "NOT_FOUND", "agy binary not found in PATH") from None

    if r.returncode != 0:
        reason = (r.stderr or r.stdout or "").strip()[:500]
        raise ProviderError("agy", r.returncode, reason)
    raw_stdout = (r.stdout or "").strip()
    if not raw_stdout:
        raise ProviderError("agy", "EMPTY", "empty stdout from agy print mode")
        
    try:
        data = json.loads(raw_stdout)
    except json.JSONDecodeError:
        raise ProviderError("agy", "BAD_JSON", raw_stdout[:300])
        
    if data.get("status") != "SUCCESS":
        raise ProviderError("agy", "BAD_JSON", raw_stdout[:300])
        
    content = data.get("response", "").strip()
    usage = data.get("usage", {})
    pin = usage.get("input_tokens", 0)
    pout = usage.get("output_tokens", 0)
    cache = usage.get("cache_read_tokens", 0)
    conv_id = data.get("conversation_id")
    _LAST_AGY_NUM_TURNS = data.get("num_turns")
    _LAST_AGY_DURATION_S = data.get("duration_seconds")
    
    return (content, model_name, conv_id, pin, pout, cache, None)


def call_xai_responses(spec: dict, key: str, history: list, system: str,
                        max_output_tokens: int = 8192, web_search: bool = True,
                        max_tool_calls: int = XAI_MAX_TOOL_CALLS):
    """POST {url}/responses — xAI's Agent Tools API. The old chat/completions
    live-search request field is 410 Gone ("Live search is deprecated.
    Please switch to the Agent Tools API") as of 2026-07; this is the
    replacement, live-verified by the architect on 2026-07-27
    (WO-ai-router-0024).

    `max_tool_calls` caps server-side web_search calls per request — see
    XAI_MAX_TOOL_CALLS above for why this is mandatory, not cosmetic.

    Response shape has NO `output_text` convenience field. Text lives at
    output[] -> item.type=="message" -> item.content[] ->
    c.type=="output_text" -> c.text (citations ride in c.annotations[] as
    url_citation). output[] also carries `reasoning` and `web_search_call`
    items, which are skipped. usage.cost_in_usd_ticks (1 tick = 1e-10 USD)
    is xAI's own billed cost and is stashed via the module-level
    _LAST_TRUE_COST for the caller to use verbatim instead of the
    token-table estimate (the table cannot see per-search-call billing).
    """
    msgs = ([{"role": "system", "content": system}] if system else []) + history
    body = {"model": spec["api"], "input": msgs, "max_output_tokens": max_output_tokens}
    if web_search:
        body["tools"] = [{"type": "web_search"}]
        body["max_tool_calls"] = max_tool_calls

    r = _post_with_retry(spec["api"], f"{spec['url']}/responses",
                         headers={"Authorization": f"Bearer {key}"}, json=body)
    d = r.json()

    if d.get("status") != "completed":
        raise ProviderError(spec["api"], 200,
            f"incomplete response: status={d.get('status')!r} "
            f"incomplete_details={d.get('incomplete_details')!r}")

    text = None
    for item in d.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") == "output_text":
                text = c.get("text")
                break
        if text is not None:
            break
    if text is None:
        raise ProviderError(spec["api"], 200, "malformed response: no output_text in output[]")

    u = d.get("usage", {})
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    tool_usage = u.get("server_side_tool_usage_details") or {}
    web_search_calls = tool_usage.get("web_search_calls", 0)
    if web_search and web_search_calls >= max_tool_calls:
        logger.warning(f"⚠️  xai web_search hit max_tool_calls={max_tool_calls} "
                        f"— answer may be truncated research")

    global _LAST_TRUE_COST
    ticks = u.get("cost_in_usd_ticks")
    _LAST_TRUE_COST = {"cost_usd": ticks / 1e10, "web_search_calls": web_search_calls} if ticks is not None else None

    return (text, d.get("model"), d.get("id"),
            u.get("input_tokens", 0), u.get("output_tokens", 0),
            cached, None)


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

For a file marked "large" below, you MUST NOT use a FILE block — use one or
more PATCH blocks instead, one per distinct edit:

===PATCH: relative/path/from/project/root.py===
===OLD===
<literal text that currently exists in the file, verbatim, including
indentation — copied EXACTLY from the CURRENT FILE content you were given>
===NEW===
<replacement text>
===END PATCH===
(repeat the PATCH block for every distinct edit; OLD must match EXACTLY ONE
location in the file — if the text you want to change appears more than
once, include enough surrounding context in OLD to make it unique)

===SUMMARY===
3-5 lines: what was done, what was NOT done, any assumption you made.
===END SUMMARY===

Rules:
- Always emit the FULL file content for a FILE block, never a partial diff.
- Only emit FILE/PATCH blocks for files you are actually changing.
- Never wrap file content in markdown fences.
- Paths are relative to the project root: no leading slash, no ".." segments.
- OLD text in a PATCH block must be copied verbatim from the file content
  you were given — never paraphrased, re-indented, or reconstructed from
  memory. If you cannot quote it exactly, use a smaller/different OLD span
  that you CAN quote exactly.
"""

_FILE_START_RE = re.compile(r"^===FILE: (.+)===$")
_FILE_END = "===END FILE==="
_PATCH_START_RE = re.compile(r"^===PATCH: (.+)===$")
_OLD_START = "===OLD==="
_NEW_START = "===NEW==="
_PATCH_END = "===END PATCH==="
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
    """Parse sentinel-line blocks. Returns (files: list[(path, content)],
    patches: list[(path, old, new)], summary: str|None).

    Regex on line starts per SPEC v1 — content between markers is written verbatim
    (a trailing newline is added by the caller if missing, never here).

    PATCH blocks (owner decree 2026-07-27, the large-file guard) are the
    mandatory alternative to a full ===FILE=== rewrite for files at/above
    LARGE_FILE_BYTES: ===PATCH: path=== / ===OLD=== / ===NEW=== /
    ===END PATCH===. `old`/`new` are joined verbatim from their line ranges,
    exactly like a FILE body — no stripping, no normalisation. A malformed
    PATCH header (missing the ===OLD=== sentinel right after it) is simply
    not treated as a patch; parsing falls through to the next line so a
    single bad block can never hang the parser.
    """
    lines = text.split("\n")
    files, patches, summary = [], [], None
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
        m2 = _PATCH_START_RE.match(line)
        if m2:
            path = m2.group(1).strip()
            i += 1
            if i >= n or lines[i].rstrip("\r") != _OLD_START:
                # Malformed: no ===OLD=== right after the header. Do not
                # consume further lines as part of this non-patch — let the
                # outer loop reprocess line i normally (no infinite loop:
                # i already advanced past the ===PATCH:...=== header line).
                continue
            i += 1  # skip ===OLD===
            old_body = []
            while i < n and lines[i].rstrip("\r") != _NEW_START:
                old_body.append(lines[i])
                i += 1
            i += 1  # skip ===NEW===
            new_body = []
            while i < n and lines[i].rstrip("\r") != _PATCH_END:
                new_body.append(lines[i])
                i += 1
            patches.append((path, "\n".join(old_body), "\n".join(new_body)))
            i += 1  # skip ===END PATCH===
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
    return files, patches, summary


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


# Owner decree 2026-07-27 (root cause of the 50KB->245-line incident): a
# cheap worker model must never full-rewrite a file this large — it must
# emit ===PATCH: blocks instead. MAX_SHRINK_RATIO catches the same failure
# mode on smaller files (a "fix" that quietly drops most of the file).
LARGE_FILE_BYTES = 12_000
MAX_SHRINK_RATIO = 0.5


def _human_size(n: int) -> str:
    return f"{n}b" if n < 1024 else f"{n / 1024:.1f}k"


def _apply_patches(patches: list, project_root: Path, allow_patterns: list):
    """Apply ===PATCH: blocks. ASSERT-style exact match ONLY: `old` must
    appear in the current file content EXACTLY ONCE, byte for byte. No
    regex, no whitespace normalisation, no "closest match" fallback — a
    fuzzy match here would silently corrupt the file, which is exactly the
    failure mode this protocol exists to prevent (owner decree 2026-07-27).
    """
    applied, rejected = [], []
    for rel, old, new in patches:
        path, err = _safe_write_path(rel, project_root, allow_patterns)
        if err:
            rejected.append((rel, err))
            continue
        if not path.exists():
            rejected.append((rel, "PATCH target does not exist — use ===FILE: for a new file"))
            continue
        content = path.read_text()
        count = content.count(old)
        if count == 0:
            rejected.append((rel, "OLD text not found verbatim"))
            continue
        if count > 1:
            rejected.append((rel, f"OLD text ambiguous ({count} matches)"))
            continue
        new_content = content.replace(old, new, 1)
        path.write_text(new_content)
        new_size = len(new_content.encode())
        # (path, resulting size, delta) — size stays an int so this list can be
        # formatted and audited with the same helpers as _write_files() output.
        applied.append((rel, new_size, new_size - len(content.encode())))
    return applied, rejected


def _write_files(files: list, project_root: Path, allow_patterns: list, allow_full_rewrite: bool = False):
    """Write ===FILE: blocks. Guards a full rewrite of an existing file per
    the owner decree 2026-07-27 large-file protocol:
      - existing size >= LARGE_FILE_BYTES: reject, the model must use
        ===PATCH: instead.
      - new size < MAX_SHRINK_RATIO * existing size: reject as a suspicious
        shrink (the actual 50KB->245-line incident pattern).
    Both guards are bypassed ONLY by allow_full_rewrite=True (CLI
    --allow-full-rewrite; deliberately NOT exposed on the MCP tool).
    """
    written, rejected = [], []
    for rel, content in files:
        path, err = _safe_write_path(rel, project_root, allow_patterns)
        if err:
            rejected.append((rel, err))
            continue
        data = content if content.endswith("\n") else content + "\n"
        new_size = len(data.encode())
        if path.exists() and not allow_full_rewrite:
            existing_size = path.stat().st_size
            if existing_size >= LARGE_FILE_BYTES:
                rejected.append((rel, f"large file ({_human_size(existing_size)}): full rewrite "
                                       f"forbidden — use ===PATCH: (owner decree 2026-07-27)"))
                continue
            if existing_size > 0 and new_size < MAX_SHRINK_RATIO * existing_size:
                rejected.append((rel, f"suspicious shrink {_human_size(existing_size)} -> "
                                       f"{_human_size(new_size)} — use ===PATCH: or --allow-full-rewrite"))
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        written.append((rel, new_size))
    return written, rejected


def _tail_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def run_verify(cmd: str, cwd: Path):
    """Run --verify. Output is captured, NEVER printed in full. Returns (ok, output, elapsed_s, returncode)."""
    t0 = time.time()
    try:
        # shell=True is deliberate and required to support shell pipelines (e.g., cmd1 | cmd2)
        # in verify commands. The caller is trusted by design, so shlex/shell=False is rejected.
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,  # noqa: PLW1510
                           text=True, timeout=VERIFY_TIMEOUT)
        ok = r.returncode == 0
        output = (r.stdout or "") + (r.stderr or "")
        returncode = r.returncode
    except subprocess.TimeoutExpired:
        ok, output = False, f"TIMEOUT after {VERIFY_TIMEOUT}s"
        returncode = None
    return ok, output, time.time() - t0, returncode


def _get_channel_system_prompt(model: str) -> str:
    if model in ("flash", "pro", "deepseek"):
        channel = "deepseek"
    elif model in ("minimax", "m3"):
        channel = "minimax"
    elif model.startswith("gemini-") or model in ("agy", "antigravity"):
        # agy IS Gemini 3.1 Pro under the hood — same vendor, same template
        # file (templates/system-prompts/gemini.md). Keep the template file
        # name as "gemini"; only the MODELS registration changed.
        channel = "gemini"
    else:
        channel = model
        
    try:
        p = Path(__file__).parent.parent / "templates" / "system-prompts" / f"{channel}.md"
        if p.exists():
            return p.read_text().strip() + "\n\n"
    except Exception:  # noqa: BLE001, S110
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
        size = len(content.encode())
        if size >= LARGE_FILE_BYTES:
            parts.append(
                f"NOTE: file {path} is large ({_human_size(size)}) — you MUST answer "
                f"with ===PATCH: blocks for it, never a full ===FILE: rewrite "
                f"(owner decree 2026-07-27).\n"
            )
    parts.append(f"Task:\n{task}\n")
    return "\n".join(parts)


def _format_worker_summary(written, rejected, verify_cmd, verify_status, attempt,
                            max_attempts, elapsed, summary, total_files, cost,
                            echoed_model, fail_tail, hit_rates, patched=()):
    def fmt_written(items):
        return ", ".join(f"{p} ({_human_size(sz)})" for p, sz in items) if items else "(none)"

    def fmt_patched(items):
        return ", ".join(
            f"{p} ({_human_size(sz)}, {'+' if d >= 0 else ''}{d}b)" for p, sz, d in items
        ) if items else "(none)"

    def fmt_rejected(items):
        return ", ".join(f"REJECTED: {p} ({reason})" for p, reason in items) if items else "(none)"

    lines = []
    if not written and not patched and total_files > 0:
        # Owner decree 2026-07-27: a run where every block was rejected
        # (e.g. every FILE was a forbidden large-file rewrite) must NOT read
        # as a quiet success — the caller must see this as a failure.
        lines.append("status        : ALL BLOCKS REJECTED — nothing written, this is a FAILURE")
    lines.extend([
        f"files written : {fmt_written(written)}",
        f"files patched : {fmt_patched(patched)}",
        f"rejected      : {fmt_rejected(rejected)}",
    ])
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
                     verify_cmd: str, retries: int, project_root: Path | None = None,
                     via: str | None = None, estimate: bool = False,
                     allow_full_rewrite: bool = False, session_key: str | None = None,
                     resume: bool = True, self_fix: bool = True) -> str:
    """Worker mode per DELEGATE-TOOL-DESIGN.md SPEC v1. Only the returned summary
    (≤25 lines) is meant to reach Claude's context — golden rule."""
    spec = MODELS[model]
    if spec["provider"] == "agy_cli":
        # agy authenticates via its own CLI session (Google AI Pro
        # subscription) — there is no env-var API key to check.
        key = ""
    else:
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

    if spec["provider"] == "gemini":
        caller = call_gemini
    elif spec["provider"] == "agy_cli":
        initial_conv_id = _get_session_conversation(session_key) if (session_key and resume) else None
        current_conv_id = [initial_conv_id]
        send_delta_only = [False]
        def caller(spec, key, history, system, max_output_tokens=8192, _root=project_root):
            # agy -p takes ONE flat text prompt, not a chat-turn array like
            # the HTTP providers. Flatten system + accumulated history
            # (retries append turns, exactly like the other providers) into
            # a single ordered text block, oldest turn first.
            if send_delta_only[0] and current_conv_id[0] is not None:
                prompt_text = history[-1]["content"]
            else:
                parts = [system + AGY_NO_TOOLS_ADDENDUM] if system else [AGY_NO_TOOLS_ADDENDUM]
                parts.extend(
                    f"[{'ASSISTANT' if m['role'] == 'assistant' else 'USER'}]\n{m['content']}"
                    for m in history
                )
                prompt_text = "\n\n".join(parts)
            kwargs = {}
            if current_conv_id[0] is not None:
                kwargs["conversation_id"] = current_conv_id[0]
            try:
                res = call_agy_print(prompt_text, spec["api"], _root, AGY_WORKER_TIMEOUT_S, **kwargs)
            except ProviderError:
                if current_conv_id[0] is not None:
                    if session_key:
                        _clear_worker_session(session_key)
                    current_conv_id[0] = None
                    res = call_agy_print(prompt_text, spec["api"], _root, AGY_WORKER_TIMEOUT_S)
                else:
                    raise
            
            new_conv_id = res[2]
            if new_conv_id is not None:
                current_conv_id[0] = new_conv_id
                if session_key:
                    _set_session_conversation(session_key, new_conv_id)
            return res
    else:
        caller = call_openai
    # Prefix discipline: system prompt (WORKER_PROTOCOL_SYSTEM) is the constant head;
    # history is append-only for retries; files come before the task string.
    history = [{"role": "user", "content": build_worker_prompt(task, file_specs, model)}]
    total_cost = 0.0
    total_pin = total_pout = total_cache = 0
    echoed_model = spec["api"]
    hit_rates = []

    def call_once():
        nonlocal total_cost, total_pin, total_pout, total_cache, echoed_model
        answer, echoed, _rid, pin, pout, cache, _cache_miss = caller(spec, key, history, WORKER_PROTOCOL_SYSTEM)

        total_cost += compute_token_cost(spec, pin, pout, cache)
        total_pin += pin
        total_pout += pout
        total_cache += cache

        echoed_model = echoed or echoed_model
        if pin > 0:
            hit_rates.append(f"{cache/pin*100:.1f}%")
        return answer

    answer = call_once()
    files, patches, summary = parse_worker_response(answer)
    if not files and not patches:
        # Protocol failure: exactly one automatic re-prompt, then fail loudly.
        history.append({"role": "assistant", "content": answer})
        history.append({"role": "user",
                        "content": "your output did not follow the FILE/PATCH protocol, re-emit"})
        answer = call_once()
        files, patches, summary = parse_worker_response(answer)
        if not files and not patches:
            sys.exit("❌ worker returned no ===FILE=== or ===PATCH=== blocks after "
                     "one re-prompt — protocol failure")
    history.append({"role": "assistant", "content": answer})

    written, rejected = _write_files(files, project_root, allow_patterns, allow_full_rewrite)
    patched, patch_rejected = _apply_patches(patches, project_root, allow_patterns)
    rejected.extend(patch_rejected)
    total_files = len(files) + len(patches)

    attempt = 1
    verify_status, elapsed, fail_output = "SKIPPED", 0.0, ""
    if verify_cmd:
        verify_max_attempts = max_attempts
        if spec["provider"] == "agy_cli":
            verify_max_attempts = min(max_attempts, 2) if self_fix else 1
            
        while True:
            ok, output, elapsed, returncode = run_verify(verify_cmd, project_root)
            verify_status = "PASS" if ok else "FAIL"
            if ok or attempt >= verify_max_attempts:
                fail_output = output if not ok else ""
                break
            
            if spec["provider"] == "agy_cli":
                tail = _redact(output[-4000:])
                content = (f"verify command failed: {verify_cmd}\n"
                           f"exit code: {returncode}\n"
                           f"output (last ~4000 chars):\n{tail}\n\n"
                           f"Fix the files and re-emit the full FILE/PATCH protocol.")
                history.append({"role": "user", "content": content})
                send_delta_only[0] = True
                try:
                    attempt += 1
                    answer = call_once()
                finally:
                    send_delta_only[0] = False
            else:
                history.append({"role": "user",
                                "content": f"verify failed:\n{_tail_lines(output, 40)}\n"
                                           f"fix the files and re-emit the full FILE/PATCH protocol."})
                attempt += 1
                answer = call_once()
                
            history.append({"role": "assistant", "content": answer})
            retry_files, retry_patches, retry_summary = parse_worker_response(answer)
            if retry_summary:
                summary = retry_summary
            if retry_files or retry_patches:
                more_written, more_rejected = _write_files(retry_files, project_root, allow_patterns, allow_full_rewrite)
                more_patched, more_patch_rejected = _apply_patches(retry_patches, project_root, allow_patterns)
                more_rejected.extend(more_patch_rejected)
                written.extend(more_written)
                patched.extend(more_patched)
                rejected.extend(more_rejected)
                total_files += len(retry_files) + len(retry_patches)

    project, commit = project_info()
    
    self_fix_rounds = 0
    self_fix_outcome = "skipped"
    if spec["provider"] == "agy_cli" and self_fix and attempt > 1:
        self_fix_rounds = 1
        self_fix_outcome = "fixed" if verify_status == "PASS" else "failed"

    # agy-only: surface the live conversation id + agy's own num_turns/duration
    # from the LAST call_agy_print() this invocation made, so the ledger row
    # itself is proof of warm-session reuse (D1: "Record num_turns and
    # duration_seconds") — not just the printed cache-hit-rate summary line.
    agy_conversation_id = current_conv_id[0] if spec["provider"] == "agy_cli" else None
    agy_num_turns = _LAST_AGY_NUM_TURNS if spec["provider"] == "agy_cli" else None
    agy_duration_s = _LAST_AGY_DURATION_S if spec["provider"] == "agy_cli" else None

    # Patched files are audited alongside written ones (path + resulting size);
    # the per-patch delta is a summary-only detail.
    audit_written = written + [(p, sz) for p, sz, _ in patched]
    # agy: $0 by subscription, tokens real — NOT cost_unknown (was a stale zero-tokens bug)
    _write_worker_audit(model, echoed_model, project, commit, audit_written, rejected,
                        verify_cmd, verify_status, attempt, total_cost, via=via,
                        cost_unknown=False, self_fix_rounds=self_fix_rounds, self_fix_outcome=self_fix_outcome,
                        agy_conversation_id=agy_conversation_id, agy_num_turns=agy_num_turns,
                        agy_duration_s=agy_duration_s, pin=total_pin, pout=total_pout, cache=total_cache)

    return _format_worker_summary(written, rejected, verify_cmd, verify_status, attempt,
                                  verify_max_attempts if verify_cmd else max_attempts,
                                  elapsed, summary, total_files, total_cost,
                                  echoed_model, fail_output, hit_rates, patched=patched)


def _write_worker_audit(model, echoed, project, commit, written, rejected,
                        verify_cmd, verify_status, attempts, cost, via=None,
                        cost_unknown=False, self_fix_rounds=0, self_fix_outcome="skipped",
                        agy_conversation_id=None, agy_num_turns=None, agy_duration_s=None,
                        pin=0, pout=0, cache=0):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_asked": model, "model_echoed": echoed,
        "session": None, "project": project, "commit": commit,
        # Raw token counts across every call this delegation made (initial +
        # any protocol/self-fix retries) — same field names as the chat-mode
        # ledger (_write_audit's "in"/"out"/"cache"). Was always 0/0/0 for
        # agy before D1 fixed call_agy_print()'s token accounting.
        "in": pin, "out": pout, "cache": cache,
        "cost_usd": round(cost, 6), "cached": False,
        "mode": "worker",
        "files_written": [p for p, _ in written],
        "files_rejected": [p for p, _ in rejected],
        "verify_cmd": verify_cmd, "verify_status": verify_status,
        "attempts": attempts,
        "self_fix_rounds": self_fix_rounds,
        "self_fix_outcome": self_fix_outcome,
    }
    # For standalone worker delegate, use MODELS quota channel if not explicitly provided
    q_channel = MODELS.get(model, {}).get("quota_channel") if model in MODELS else None
    if q_channel:
        rec["quota_channel"] = q_channel

    if via is not None:
        rec["via"] = via
    if cost_unknown:
        rec["cost_unknown"] = True
    # agy-only warm-session proof fields (D1/D2): the live conversation id and
    # agy's own reported num_turns/duration_seconds for the last call this
    # delegation made — absent entirely for non-agy providers.
    if agy_conversation_id is not None:
        rec["agy_conversation_id"] = agy_conversation_id
    if agy_num_turns is not None:
        rec["agy_num_turns"] = agy_num_turns
    if agy_duration_s is not None:
        rec["agy_duration_s"] = agy_duration_s
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def worker_delegate(task: str, model: str, files_arg: str, allow_write_arg: str,
                     verify_cmd: str, retries: int, project_root: Path | None = None,
                     via: str | None = None, estimate: bool = False,
                     allow_full_rewrite: bool = False, session_key: str | None = None,
                     resume: bool = True, self_fix: bool = True) -> str:
    # Resolve here, not only in the CLI: `agy` stopped being a MODELS key when
    # the catalog was opened (it is now an ALIAS for gemini-3.1-pro-high), and
    # callers that bypass the CLI — the MCP server above all — hand us the raw
    # alias. resolve_model() is idempotent, so a canonical name passes through.
    model = resolve_model(model)
    ch = get_model_channel(model)
    if not is_channel_enabled(ch):
        msg = f"channel {ch} disabled in channels.json"
        print(msg)
        logger.warning(msg)
        raise ValueError(f"All candidates disabled (last tried: {ch})")

    try:
        return _worker_delegate_inner(task, model, files_arg, allow_write_arg, verify_cmd, retries, project_root, via, estimate, allow_full_rewrite, session_key, resume, self_fix)
    except ProviderError as e:
        # Owner decree 2026-07-27: silent escalation from a $0 channel to a
        # PAID one is exactly the "silent overspend" this project bans. No
        # automatic fallback of any kind — the caller must explicitly accept
        # the spend by re-running with a named paid model.
        raise ValueError(
            f"{model} unavailable ({e}). No automatic paid fallback (owner "
            f"decree 2026-07-27). Re-run explicitly with model='flash'|'pro'"
            f"|'minimax' if you accept the spend."
        ) from e

def _delegate_inner(prompt: str, model: str, session: str = "", system: str = "",
             use_cache: bool = True, max_output_tokens: int = 8192,
             via: str | None = None, estimate: bool = False,
             web_search: bool = True, max_tool_calls: int = XAI_MAX_TOOL_CALLS) -> str:
    model = resolve_model(model)  # same reason as worker_delegate(): aliases reach here too
    spec = MODELS[model]
    if spec["provider"] == "agy_cli":
        # Same reason as the worker path: agy authenticates through its own CLI
        # session against the Google AI Pro subscription, so there is no env-var
        # API key. Without this branch every agy model failed here with an empty
        # key name ("❌  not set in vault .env"), which made the free Claude pool
        # unreachable from the chat/route path even after the catalog was opened.
        key = ""
    else:
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
    if spec["provider"] == "gemini":
        caller = call_gemini
    elif spec["provider"] == "xai":
        def caller(s, k, h, sy, max_output_tokens=8192):
            return call_xai_responses(s, k, h, sy, max_output_tokens=max_output_tokens,
                                       web_search=web_search, max_tool_calls=max_tool_calls)
    elif spec["provider"] == "agy_cli":
        def caller(s, k, h, sy, max_output_tokens=8192):
            # agy -p takes ONE flat text prompt, not a chat-turn array. Flatten
            # system + history into a single ordered block, oldest turn first —
            # same shape the worker path uses. No AGY_NO_TOOLS_ADDENDUM here:
            # that addendum exists to keep the worker protocol's file-writing
            # discipline, and this is a plain chat call with no such protocol.
            parts = [sy] if sy else []
            parts.extend(
                f"[{'ASSISTANT' if m['role'] == 'assistant' else 'USER'}]\n{m['content']}"
                for m in h
            )
            return call_agy_print("\n\n".join(parts), s["api"], Path.cwd())
    else:
        caller = call_openai
    answer, echoed, rid, pin, pout, cache, cache_miss = caller(
        spec, key, history, system, max_output_tokens=max_output_tokens)

    dt_s = time.time() - t0

    true_cost = _pop_last_true_cost()
    web_search_calls = None
    if true_cost is not None:
        cost = true_cost["cost_usd"]
        web_search_calls = true_cost["web_search_calls"]
    else:
        cost = compute_token_cost(spec, pin, pout, cache)

    print(format_proof(echoed, rid, pin, pout, cache, cost, dt_s, spec["cin"] == 0))

    if session:
        history.append({"role": "assistant", "content": answer})
        save_history(session, history)

    if cache_key:
        cache_put(cache_key, model, prompt, answer)

    _write_audit(model, echoed, rid, session, project, commit, pin, pout,
                 cache, cost, dt_s, cached=False, via=via, cache_miss=cache_miss,
                 web_search_calls=web_search_calls)
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
             via: str | None = None, estimate: bool = False,
             web_search: bool = True, max_tool_calls: int = XAI_MAX_TOOL_CALLS) -> str:
    ch = get_model_channel(model)
    if not is_channel_enabled(ch):
        msg = f"channel {ch} disabled in channels.json"
        print(msg)
        logger.warning(msg)
        raise ValueError(f"All candidates disabled (last tried: {ch})")

    try:
        return _delegate_inner(prompt, model, session, system, use_cache, max_output_tokens, via, estimate, web_search, max_tool_calls)
    except ProviderError as e:
        # Owner decree 2026-07-27: no automatic fallback of any kind — see
        # the identical rationale in worker_delegate() above.
        raise ValueError(
            f"{model} unavailable ({e}). No automatic paid fallback (owner "
            f"decree 2026-07-27). Re-run explicitly with model='flash'|'pro'"
            f"|'minimax' if you accept the spend."
        ) from e

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
        model_name = model or "gemini-3.1-pro-high"
        provider_model = MODELS.get(model_name)
        if not provider_model:
            raise ValueError(f"Unknown agy model: {model_name}")
        quota_channel = provider_model.get("quota_channel", "google-ai-pro")
        spec = provider_model
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
        # gpt-5-mini bills at 0x premium on Copilot Pro (see
        # copilot_multipliers.json in the data dir for the live table);
        # escalate per-task via --model gpt-5 / claude-sonnet-4.5.
        model_name = model or "gpt-5-mini"
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
            return subprocess.run(["git", "-C", str(project_root), "status", "--porcelain"], capture_output=True, text=True, timeout=GIT_TIMEOUT).stdout.strip()  # noqa: PLW1510
        except Exception:  # noqa: BLE001
            return ""

    def _is_git_repo():
        try:
            r = subprocess.run(["git", "-C", str(project_root), "rev-parse", "--is-inside-work-tree"],  # noqa: PLW1510
                               capture_output=True, text=True, timeout=GIT_TIMEOUT)
            return r.returncode == 0 and r.stdout.strip() == "true"
        except Exception:  # noqa: BLE001
            return False

    def _fs_snapshot():
        # Fallback change-detection for a NON-git workdir. git status sees
        # nothing outside a repo, so without this the router reported 0 files
        # changed even when the runner wrote — the false "agy hallucinated"
        # signal (2026-07-21). Map each file to (size, mtime_ns); .git skipped.
        snap = {}
        for root, dirs, files in os.walk(project_root):
            if ".git" in dirs:
                dirs.remove(".git")
            for fn in files:
                p = Path(root) / fn
                try:
                    st = p.stat()
                    snap[str(p.relative_to(project_root))] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
        return snap

    git_mode = _is_git_repo()
    status_before = _git_status() if git_mode else ""
    snap_before = {} if git_mode else _fs_snapshot()

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
        # --add-dir <project_root>: WITHOUT it, agy (antigravity-cli) does not
        # reliably write to the cwd we hand it — it non-deterministically
        # sandboxes writes into ~/.gemini/antigravity-cli/scratch/ and then
        # reports success, so the file never reaches the target and the router
        # sees 0 changes. This was the real "agy did the work but 0 files
        # changed / fabricated hash" signal (verified 2026-07-21: file landed
        # in scratch, not cwd). Binding the workdir into agy's workspace makes
        # it write there; probed git + non-git workdirs, 3/3 landed in cwd.
        # No --effort here either, same reason as call_agy_print(): the effort
        # level is part of the model id, and agy rejects the flag outright for
        # the Claude ids. `provider_model["api"]` already carries it.
        cmd = ["agy", "-p", task, "--model", provider_model["api"], "--mode", "accept-edits",
               "--dangerously-skip-permissions", "--add-dir", str(project_root),
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
            proc = subprocess.Popen(cmd, cwd=project_root, stdout=f, stderr=subprocess.STDOUT, preexec_fn=os.setsid, env=run_env)  # noqa: PLW1509
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait()
            timed_out = True
        except FileNotFoundError:
            sys.exit(f"❌ {runner} binary not found in PATH")

    elapsed = time.time() - t0
    
    files_changed = []
    if git_mode:
        status_after = _git_status()
        before_lines = set(status_before.splitlines())
        after_lines = set(status_after.splitlines())
        for line in (after_lines - before_lines):
            files_changed.append(line)  # noqa: PERF402
    else:
        snap_after = _fs_snapshot()
        for rel, sig in snap_after.items():
            if snap_before.get(rel) != sig:
                files_changed.append(f" A {rel}")
        for rel in set(snap_before) - set(snap_after):
            files_changed.append(f" D {rel}")

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
        except Exception:  # noqa: BLE001, S110
            pass
        if cost_unknown:
            # Fallback: audit-log rollup, window sized to this run (metrics
            # --since takes durations like 30m; 1m would miss a long run).
            since = f"{int(elapsed // 60) + 2}m"
            try:
                r = subprocess.run(["codewhale", "metrics", "--json", "--since", since], capture_output=True, text=True, timeout=5)  # noqa: PLW1510
                if r.returncode == 0:
                    data = json.loads(r.stdout)
                    if "cost_usd" in data:
                        cost_usd = float(data["cost_usd"])
                        cost_unknown = False
            except Exception:  # noqa: BLE001, S110
                pass

    verify_status = "SKIPPED"
    verify_elapsed = 0.0
    fail_output = ""
    if verify_cmd and not timed_out:
        ok, vout, verify_elapsed, _rc = run_verify(verify_cmd, project_root)
        verify_status = "PASS" if ok else "FAIL"
        if not ok:
            fail_output = vout
            
    if runner == "copilot":
        # Multiplier comes from the vault config (copilot_multipliers.json);
        # 0x models don't consume the monthly premium-request allowance.
        m = copilot_premium_multiplier(model_name)
        premium_req = int(m) if m == int(m) else m
    else:
        premium_req = None
            
    _write_agent_audit(model_name, model_name, project, commit, len(files_changed), verify_status, cost_usd, cost_unknown, quota_channel, via=via, runner=runner, exit_code=exit_code, run_id=run_id, premium_requests=premium_req)

    if timed_out:
        status = "TIMEOUT — process group killed"
    elif exit_code != 0:
        status = f"FAILED (exit {exit_code})"
    elif verify_status == "FAIL":
        # Exit 0 but the independent check we ran said otherwise — do not let
        # a green exit code mask a failed verify.
        status = "COMPLETED — ⚠️ VERIFY FAILED"
    elif len(files_changed) == 0 and verify_status == "SKIPPED":
        # Silent-success hole: unlike the worker path (where the router itself
        # writes the model's ===FILE=== blocks to disk), the agent path relies
        # on the CLI's own tool-calls to change files and only diffs git after.
        # A clean exit with zero filesystem changes and no independent verify
        # therefore proves nothing — the CLI may have printed a plan or a
        # fabricated success (e.g. an invented commit hash) without writing.
        # Surface it as UNVERIFIED instead of a trustworthy COMPLETED.
        status = "COMPLETED — ⚠️ 0 files changed, UNVERIFIED (runner self-report not trusted; pass --verify to confirm)"
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



def send_to_owner(files: list[str], title: str) -> str:
    """Send files to the owner's Telegram as documents."""
    token = os.environ.get("AI_ROUTER_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("AI_ROUTER_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not in env")

    if not files:
        raise ValueError("No files provided")

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    
    first = True
    msg_ids = []

    with httpx.Client(timeout=30.0) as client:
        for filepath in files:
            p = Path(filepath)
            if not p.is_absolute():
                raise ValueError(f"Path must be absolute: {filepath}")
            if not p.is_file():
                raise ValueError(f"File not found: {filepath}")

            data = {"chat_id": chat_id}
            if first and title:
                data["caption"] = title
                first = False

            with open(p, "rb") as f:
                files_payload = {"document": (p.name, f)}
                try:
                    resp = client.post(url, data=data, files=files_payload)
                    resp.raise_for_status()
                    result = resp.json()
                except httpx.HTTPError as e:
                    err_msg = str(e).replace(token, "<redacted>")
                    raise RuntimeError(f"Telegram API error: {err_msg}") from None
            # A 200 with ok=false (or no message_id) is still a failure —
            # never report success without the message_id proof.
            msg_id = result.get("result", {}).get("message_id")
            if not result.get("ok") or msg_id is None:
                desc = str(result.get("description", "no description")).replace(token, "<redacted>")
                raise RuntimeError(f"Telegram refused {p.name}: {desc}")
            msg_ids.append(msg_id)

    return "message_id=" + ",".join(str(m) for m in msg_ids)


def send_note(to_project: str, message: str, priority: str = "normal", subject: str = "", notify: bool = True) -> str:
    """Send a note to another project's inbox."""
    if priority not in ("low", "normal", "high"):
        priority = "normal"
    
    if ".." in to_project or "/" in to_project or "\\" in to_project:
        raise ValueError(f"invalid project name: {to_project}")
        
    target_root = (AGENT_PROJECTS / to_project).resolve()
    
    if not target_root.is_relative_to(AGENT_PROJECTS) or target_root == AGENT_PROJECTS:
        raise ValueError(f"invalid project path: {to_project}")
        
    if not target_root.exists() or not target_root.is_dir():
        raise ValueError(f"unknown project: {to_project}")
        
    inbox_dir = target_root / "workspace" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    
    from_project, _ = project_info()
    
    now = dt.datetime.now().astimezone()
    slug_raw = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in (subject or "note"))
    slug = re.sub(r"-+", "-", slug_raw).strip("-")[:20] or "note"
    filename = f"NOTE-{now.strftime('%Y-%m-%d-%H%M%S')}-{from_project}-{slug}.md"
    file_path = inbox_dir / filename
    
    redacted_message = _redact(message)
    
    content_note = f"""---
from: {from_project}
to: {to_project}
created: {now.isoformat(timespec='seconds')}
priority: {priority}
read: false"""
    if subject:
        content_note += f"\nsubject: {subject}"
    content_note += f"""
---

{redacted_message}
"""
    
    file_path.write_text(content_note)
    
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": now.isoformat(timespec="seconds"),
        "mode": "note",
        "action": "send",
        "from": from_project,
        "to": to_project,
        "priority": priority,
        "bytes": len(redacted_message.encode("utf-8"))
    }
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
        
    if notify:
        bot_token = os.environ.get("AI_ROUTER_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
        if bot_token and chat_id:
            try:
                import dashboards
                ping_text = f"🔔 note → {to_project} · from {from_project} · {priority}\n"
                if subject:
                    ping_text += f"{subject}\n"
                ping_text += redacted_message[:200]
                key_base = f"{from_project}:{subject if subject else redacted_message[:200]}"
                dedupe_key = hashlib.sha256(key_base.encode("utf-8")).hexdigest()
                dashboards.send_note_ping_deduped(ping_text, dedupe_key)
                dashboards.push_dashboard("inbox")
            except Exception as e:
                logger.warning(f"Telegram notification failed: {e}")

    return f"note sent to {to_project} inbox ({file_path.name})"


def list_notes(project: str, unread_only: bool = True, peek: bool = False):
    """List notes for a project, optionally marking them as read."""
    if ".." in project or "/" in project or "\\" in project:
        raise ValueError(f"invalid project name: {project}")
    target_root = (AGENT_PROJECTS / project).resolve()
    
    if not target_root.is_relative_to(AGENT_PROJECTS) or target_root == AGENT_PROJECTS:
        raise ValueError(f"invalid project path: {project}")
        
    inbox_dir = target_root / "workspace" / "inbox"
    if not inbox_dir.exists():
        return []
        
    notes = []
    for f in inbox_dir.glob("NOTE-*.md"):
        content_text = f.read_text()
        if content_text.startswith("---\n"):
            header_end = content_text.find("\n---\n", 4)
            if header_end != -1:
                header = content_text[4:header_end]
                meta = {}
                for line in header.split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
                if unread_only and meta.get("read") == "true":
                    continue
                notes.append((f, meta, content_text[header_end+5:]))
                
    notes.sort(key=lambda x: x[1].get("created", ""), reverse=True)
    
    if not peek and notes:
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        for f, meta, _ in notes:
            old_content = f.read_text()
            new_content = old_content.replace("\nread: false", "\nread: true", 1)
            f.write_text(new_content)
            
            AUDIT.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": now,
                "mode": "note",
                "action": "read",
                "from": meta.get("from", "unknown"),
                "to": project,
            }
            with AUDIT.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
                
    return notes


def parse_task_note_file(path: Path) -> dict:
    content = path.read_text()
    meta = {}
    body = content
    if content.startswith("---\n"):
        header_end = content.find("\n---\n", 4)
        if header_end != -1:
            header = content[4:header_end]
            for line in header.split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = content[header_end+5:].strip()

    repo = None
    goal_lines = []
    
    for line in body.split("\n"):
        if line.startswith("repo:"):
            repo = line[5:].strip()
        else:
            goal_lines.append(line)
            
    if not repo:
        raise ValueError("task-note body missing 'repo: <path>'")
        
    return {
        "repo": repo,
        "goal": "\n".join(goal_lines).strip(),
        "constraints": meta.get("constraints", ""),
        "priority": meta.get("priority", "normal"),
        "from_project": meta.get("from")
    }


def route_task(task_note: dict, verify_cmd: str = "") -> str:
    goal = task_note.get("goal", "")
    constraints = task_note.get("constraints", "")
    repo = task_note.get("repo")

    if not repo:
        raise ValueError("task-note missing repo")

    combined_check = (goal + " " + constraints).lower()
    for word in ("push", "git push", "merge", "git merge"):
        if word in combined_check:
            raise ValueError("route_task refuses: task-note requests push/merge, which stays behind the architect/owner gate")

    full_goal = goal
    if constraints:
        full_goal += f"\nConstraints:\n{constraints}"

    report = ""
    fallback_needed = False
    fallback_reason = ""

    try:
        report = agent_delegate(task=full_goal, runner="agy", workdir=repo, verify_cmd=verify_cmd)
        if "VERIFY FAILED" in report:
            fallback_needed = True
            fallback_reason = "verify failed"
    except (Exception, SystemExit) as e:  # noqa: BLE001
        # SystemExit covers check_budget()'s daily-cap/quota-exceeded abort
        # inside agent_delegate (sys.exit, not a raised Exception) — a quota
        # hit must fall through to the paid ladder step too, per the WO's
        # "verify fail OR free-tier quota hit" escalation trigger.
        report = f"agy run failed with exception: {e}"
        fallback_needed = True
        fallback_reason = f"exception: {e}"
        
    if fallback_needed:
        try:
            fallback_report = agent_delegate(task=full_goal, runner="codewhale", model="flash", workdir=repo, verify_cmd=verify_cmd)
            report = f"Original agy run failed ({fallback_reason}). Paid fallback used (codewhale flash):\n\n{fallback_report}"
        except Exception as e:  # noqa: BLE001
            report = f"Original agy run failed ({fallback_reason}). Paid fallback (codewhale flash) also failed: {e!s}"
            
    from_project = task_note.get("from_project")
    if from_project:
        send_note(to_project=from_project, message=report, priority=task_note.get("priority", "normal"), subject="task-note result")
        
    return report

def cmd_channels(enable_channel=None, disable_channel=None):
    import shutil
    channels_json = DATA_DIR / "channels.json"
    
    if enable_channel or disable_channel:
        data = {}
        if channels_json.exists():
            try:
                import json
                data = json.loads(channels_json.read_text())
            except Exception:  # noqa: BLE001, S110
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
        except Exception:  # noqa: BLE001, S110
            pass
            
    env_disabled = [c.strip() for c in os.environ.get("AI_ROUTER_DISABLE_CHANNELS", "").split(",") if c.strip()]
    
    for ch in ["agy", "codex", "copilot", "codewhale", "google-ai-pro", "deepseek", "minimax", "grok"]:
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
                        r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=2)  # noqa: PLW1510
                        if r.returncode == 0:
                            auth_str = r.stdout.strip().split("\n")[0]
                    except Exception:  # noqa: BLE001, S110
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
                    help="model or alias (minimax|flash|pro|grok|agy or full names); "
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
    ap.add_argument("--no-search", action="store_true",
                    help="xai models only: force the plain chat path, skip the "
                         "server-side web_search tool ($0.005/call) — default is "
                         "search ON for xai models")
    ap.add_argument("--max-tool-calls", type=int, default=XAI_MAX_TOOL_CALLS,
                    help=f"xai models only: cap server-side web_search calls per "
                         f"request (default {XAI_MAX_TOOL_CALLS} — an uncapped live "
                         f"question once made 15 calls and cost $0.389)")
    ap.add_argument("--files", default="",
                    help="worker mode: comma-separated files to read/rewrite")
    ap.add_argument("--allow-write", default="",
                    help="worker mode: comma-separated globs (relative to cwd) the "
                         "worker is allowed to write; no flag = no writes")
    ap.add_argument("--verify", default="",
                    help="worker mode: shell command run after writing (never guessed)")
    ap.add_argument("--retries", type=int, default=1,
                    help="worker mode: verify-failure retries (default 1, max 2)")
    ap.add_argument("--no-self-fix", action="store_true",
                    help="disable the one-round agy self-fix retry on verify failure")
    ap.add_argument("--allow-full-rewrite", action="store_true",
                    help="worker mode: bypass the large-file/shrink guard and allow a "
                         "full ===FILE: rewrite of a file >=12KB or a >50%% shrink "
                         "(dangerous — this guard exists because of the 2026-07-27 "
                         "50KB-to-245-line truncation incident; use only if you mean it)")
    ap.add_argument("--session-key", default="", help="worker mode: session key to resume conversation")
    ap.add_argument("--worker-sessions", action="store_true", help="list all worker sessions and exit")
    ap.add_argument("--worker-sessions-clear", nargs="?", const="__ALL__", default=None,
                    help="clear worker sessions (bare flag clears all, or specify a key to clear one)")
    ap.add_argument("--note", help="send a note to the specified project inbox")
    ap.add_argument("--inbox", action="store_true", help="list unread notes in the current project inbox")
    ap.add_argument("--peek", action="store_true", help="peek at the inbox (show count/subjects, don't mark read)")
    ap.add_argument("--subject", default="", help="subject for the note")
    ap.add_argument("--priority", default="normal", help="priority for the note (low|normal|high)")
    ap.add_argument("--agent", action="store_true", help="agent mode: use agy or codewhale exec for multi-step exploration")
    ap.add_argument("--runner", default="agy", help="agent mode runner: agy (default) or codewhale")
    ap.add_argument("--timeout", type=int, default=600, help="agent mode: timeout in seconds (default 600, max 1800)")
    ap.add_argument("--route-task", help="path to a note file to execute via route_task")
    ap.add_argument("--send-to-owner", action="store_true", help="send files to owner's telegram")
    ap.add_argument("--file", action="append", default=[], help="file to send to owner (repeatable, absolute path)")
    ap.add_argument("--title", default="", help="title (caption) for the file being sent")
    ap.add_argument("--dashboard", choices=["inbox", "tasks", "both"], default=None,
                    help="push the given pinned Telegram dashboard(s) (edits in place)")
    ap.add_argument("--dashboard-dry-run", choices=["inbox", "tasks", "both"], default=None,
                    help="render the given dashboard(s) to stdout — no Telegram API call")
    a = ap.parse_args()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter())
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

    if a.worker_sessions:
        sessions = _load_worker_sessions()
        if not sessions:
            print("No worker sessions found.")
        else:
            print(f"{'SESSION KEY':<25} | {'CONVERSATION ID':<36} | AGE")
            print("-" * 80)
            now = time.time()
            for key, data in sorted(sessions.items()):
                conv_id = data.get("conversation_id", "")
                age_s = now - data.get("ts", 0)
                if age_s < 120:
                    age_str = f"{int(age_s)}s"
                elif age_s < 7200:
                    age_str = f"{int(age_s/60)}m"
                else:
                    age_str = f"{int(age_s/3600)}h"
                print(f"{key:<25} | {conv_id:<36} | {age_str}")
        return

    if a.worker_sessions_clear is not None:
        key_to_clear = None if a.worker_sessions_clear == "__ALL__" else a.worker_sessions_clear
        _clear_worker_session(key_to_clear)
        if key_to_clear:
            print(f"↺ cleared worker session '{key_to_clear}'")
        else:
            print("↺ cleared all worker sessions")
        return

    if a.inbox:
        try:
            curr_proj, _ = project_info()
            notes = list_notes(curr_proj, unread_only=True, peek=a.peek)
            if a.peek:
                if notes:
                    print(f"📬 {len(notes)} unread notes from manager")
                    for _, meta, _ in notes:
                        if meta.get("subject"):
                            print(f"  - {meta['subject']}")
                # If peek and 0 notes, print nothing as specified
            else:
                if not notes:
                    print("inbox is empty.")
                for i, (f, meta, body) in enumerate(notes, 1):
                    subj = f" - {meta.get('subject')}" if meta.get("subject") else ""
                    print(f"[{i}/{len(notes)}] note from {meta.get('from', 'unknown')} ({meta.get('created', '')}){subj}")
                    print(body.strip())
                    print("-" * 40)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"❌ {e}")
        return

    if a.note:
        load_env()
        if not a.prompt:
            sys.exit("❌ need -p PROMPT or message string for the note body")
        try:
            print(send_note(a.note, a.prompt, priority=a.priority, subject=a.subject))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"❌ {e}")
        return

    if a.route_task:
        try:
            task_note = parse_task_note_file(Path(a.route_task))
            print(route_task(task_note, verify_cmd=a.verify))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"❌ {e}")
        return

    if a.send_to_owner:
        load_env()
        try:
            print(send_to_owner(a.file, a.title))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"❌ {e}")
        return

    if a.dashboard_dry_run:
        import dashboards  # lazy: see circular-import note
        kinds = ["inbox", "tasks"] if a.dashboard_dry_run == "both" else [a.dashboard_dry_run]
        for k in kinds:
            text = dashboards.render_inbox() if k == "inbox" else dashboards.render_tasks()
            print(f"===== {k} =====")
            print(text)
        return

    if a.dashboard:
        load_env()
        import dashboards  # lazy
        kinds = ["inbox", "tasks"] if a.dashboard == "both" else [a.dashboard]
        try:
            for k in kinds:
                print(f"{k}: {dashboards.push_dashboard(k)}")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"❌ {e}")
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
        print(worker_delegate(prompt, model, a.files, a.allow_write, a.verify, a.retries,
                              estimate=a.estimate, allow_full_rewrite=a.allow_full_rewrite,
                              session_key=a.session_key or None, self_fix=not a.no_self_fix))
        return

    use_cache = not a.no_cache
    answer = delegate(prompt, model, a.session, a.system, use_cache=use_cache, estimate=a.estimate,
                       web_search=not a.no_search, max_tool_calls=a.max_tool_calls)

    if a.out:
        Path(a.out).write_text(answer)
        print(f"answer written → {a.out} ({len(answer)} chars)")
    else:
        print(answer)


if __name__ == "__main__":
    main()
