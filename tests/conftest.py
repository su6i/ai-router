"""Test-suite guardrails.

The default here is *no side effects on the outside world*. `send_note()` pings
Telegram whenever `AI_ROUTER_BOT_TOKEN` and `TELEGRAM_OWNER_CHAT_ID` are both in
the environment, and the developer shell has them both. That made every `pytest`
run deliver the suite's fixture messages ("test message", "Subject 1", "hello
audit", ...) to the owner's real chat, and write to the real dashboard state file.

Stripping the credentials for every test closes that at the only layer that
cannot be forgotten. Tests that exercise the Telegram paths set their own fake
values with `monkeypatch.setenv` and mock the HTTP client; autouse fixtures run
before test-requested ones, so those still work.
"""

import os
import pytest
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote

def _load_real_dsn():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from delegate import load_env
    load_env()
    return os.environ.get("POSTGRES_DSN")

_ORIG_DSN = _load_real_dsn()

def _pg_available() -> bool:
    if not _ORIG_DSN:
        return False
    try:
        import psycopg
        psycopg.connect(_ORIG_DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False

has_pg = _pg_available()
requires_pg = pytest.mark.skipif(not has_pg, reason="Missing Postgres")

# `rules_index.ingest()` reads its corpus from the CWD, and `.agent/constitution`
# is an untracked local symlink to the central clone -- so it is absent inside a
# `git worktree` checkout. Tests that ingest or retrieve real rule text need it
# present; without it they are asserting on a docs-only corpus, which is a
# missing fixture, not a result. `ingest()` itself now refuses that case loudly.
from pathlib import Path as _Path  # noqa: E402

has_rules_corpus = any((_Path.cwd() / ".agent" / "constitution" / "rules").glob("*.md"))
requires_rules_corpus = pytest.mark.skipif(
    not has_rules_corpus, reason="Missing .agent/constitution/rules corpus (git worktree?)"
)

# Fail loudly at collection time if a DB-touching test would run against the
# live search_path — no silent fallback to the real DB ever.
os.environ["POSTGRES_DSN"] = "postgresql://0.0.0.0:0/invalid_db_no_silent_fallback"
os.environ["PGDATABASE"] = "invalid_db_no_silent_fallback"
os.environ["PGHOST"] = "0.0.0.0"

@pytest.fixture(scope="session", autouse=True)
def isolate_pg_schema():
    if not has_pg or not _ORIG_DSN:
        yield
        return

    import psycopg
    
    with psycopg.connect(_ORIG_DSN, autocommit=True) as conn:
        if os.environ.get("RAG_TEST_SCHEMA_RESET") == "1":
            conn.execute("DROP SCHEMA IF EXISTS ai_router_test CASCADE")
        conn.execute("CREATE SCHEMA IF NOT EXISTS ai_router_test")

    parts = list(urlparse(_ORIG_DSN))
    query = parse_qs(parts[4])
    if "options" in query:
        query["options"] = [query["options"][0] + " -c search_path=ai_router_test,public"]
    else:
        query["options"] = ["-c search_path=ai_router_test,public"]
    # quote_via=quote (not the urlencode default quote_plus) so the space
    # inside "-c search_path=..." becomes %20, not a literal "+" — libpq's
    # conninfo URI parser percent-decodes but does not treat "+" as space,
    # so a literal "+" reaches Postgres as part of the GUC name and fails
    # with "unrecognized configuration parameter \"+search_path\"".
    parts[4] = urlencode(query, doseq=True, quote_via=quote)
    test_dsn = urlunparse(parts)

    with pytest.MonkeyPatch.context() as m:
        m.setenv("POSTGRES_DSN", test_dsn)
        yield

_LIVE_CREDENTIALS = ("AI_ROUTER_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID")


@pytest.fixture(autouse=True)
def no_live_telegram(monkeypatch):
    for var in _LIVE_CREDENTIALS:
        monkeypatch.delenv(var, raising=False)


# T-949: AI_ROUTER_REVIEWER (the reviewing session's identity) and
# AI_ROUTER_IN_WORKER (the delegated-worker marker) are both plausibly set in
# a real dev/CI shell -- the reviewing session sets AI_ROUTER_REVIEWER for
# real --score calls. A test that relies on either being ABSENT must not
# inherit whatever the host happens to have (same T-946 class as
# no_live_telegram above); each test controls these two explicitly via its
# own monkeypatch.setenv/delenv.
@pytest.fixture(autouse=True)
def no_host_score_guard_env(monkeypatch):
    for var in ("AI_ROUTER_REVIEWER", "AI_ROUTER_IN_WORKER"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def reset_e5_singleton():
    """Undo the process-wide model cache between tests.

    `rules_index.get_model()` memoises the model in the module-global `_MODEL`.
    Tests that swap `E5Model` for a fake (test_output_cap,
    test_stale_index_warning) reach `get_model()` while the fake is installed,
    so the FAKE lands in `_MODEL` — and monkeypatch's teardown restores the
    class, not the cache. Every later test in the same process then embeds with
    the fake's zero vector, which silently turns any similarity ranking into
    arbitrary order.

    That is not hypothetical: `pytest tests/test_rules_index.py::test_retrieval_sanity`
    passed while `pytest tests/test_rules_index.py` failed, on the same index,
    purely because of this leak.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import rules_index

    rules_index._MODEL = None
    yield
    rules_index._MODEL = None


# The vault itself -- `<vault>/data/` and `<vault>/secrets/` -- is an
# outside-world dependency exactly like the live `agy models` catalog below:
# state that differs between the developer's machine and CI, and files a
# careless test can mutate for real. `delegate.VAULT`, `DATA_DIR`,
# `SECRETS_DIR`, and every path derived from `DATA_DIR` (`AUDIT`, `BUDGETS`,
# `SESSIONS`, `CACHE`, `AGY_CATALOG_CACHE`, `WORKER_SESSIONS`) are bound at
# `delegate.py` IMPORT time, so setting `AI_ROUTER_DATA_DIR` alone does
# nothing for the already-imported module -- the derived module attributes
# are patched directly below. `ingest.py` additionally does
# `from delegate import AUDIT, DATA_DIR`, which creates its OWN name bindings
# in `ingest`'s namespace, decoupled from `delegate.AUDIT`/`delegate.DATA_DIR`
# after that import: `tests/test_ingest.py::test_integration_ingest_idempotent`
# called `ingest()` directly and wrote a real `last_ingest.json` into the
# owner's vault on every run (T-943 audit) precisely because patching only
# `delegate.AUDIT`/`delegate.DATA_DIR` would have missed it -- `ingest`'s own
# bindings are patched here too.
#
# This subsumes T-941's `frozen_agy_catalog`: `AGY_CATALOG_CACHE` now always
# lives under this same isolated vault, seeded once below with the same
# fixture data, so the standalone fixture is removed (see CHANGELOG).
_AGY_CATALOG_FIXTURE = [
    "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    "gemini-3.1-pro-high", "gemini-3.1-pro-low",
    "claude-sonnet-4-6", "gpt-oss-120b-medium",
]


@pytest.fixture(scope="session", autouse=True)
def isolate_vault(tmp_path_factory):
    """No test reads or writes the owner's real vault. See module comment above.

    Session-scoped: these constants are frozen once per process, exactly like
    the real module load they stand in for. A test that needs its own state
    (e.g. `test_budgets.py`) still patches these same names again with its own
    `monkeypatch`/`tmp_path` -- that patch is applied after this one and wins
    for the duration of that test, then reverts to *this* fixture's value on
    teardown, never to the real path.
    """
    import json
    import time
    import delegate
    import ingest

    vault_dir = tmp_path_factory.mktemp("ai-router-vault")
    data_dir = vault_dir / "data"
    secrets_dir = vault_dir / "secrets"
    data_dir.mkdir()
    secrets_dir.mkdir()

    audit = data_dir / "audit.log"
    agy_catalog = data_dir / "agy_models.json"
    agy_catalog.write_text(json.dumps(
        {"fetched_at": time.time(), "models": _AGY_CATALOG_FIXTURE}) + "\n")

    with pytest.MonkeyPatch.context() as m:
        # For any test that spawns a fresh subprocess and lets it inherit the
        # parent environment -- a fresh `import delegate` in that process
        # resolves AI_ROUTER_DATA_DIR itself, no per-test override needed.
        m.setenv("AI_ROUTER_DATA_DIR", str(vault_dir))

        m.setattr(delegate, "VAULT", vault_dir)
        m.setattr(delegate, "DATA_DIR", data_dir)
        m.setattr(delegate, "SECRETS_DIR", secrets_dir)
        m.setattr(delegate, "AUDIT", audit)
        m.setattr(delegate, "BUDGETS", data_dir / "budgets.json")
        m.setattr(delegate, "SESSIONS", data_dir / "sessions")
        m.setattr(delegate, "CACHE", data_dir / "cache.db")
        m.setattr(delegate, "AGY_CATALOG_CACHE", agy_catalog)
        m.setattr(delegate, "WORKER_SESSIONS", data_dir / "worker_sessions.json")

        # `ingest.py`'s own `from delegate import AUDIT, DATA_DIR` bindings --
        # see the module comment above.
        m.setattr(ingest, "AUDIT", audit)
        m.setattr(ingest, "DATA_DIR", data_dir)

        yield


# External CLI stubs (T-946 CI fix). Host-independent binary resolution:
# ensures test suite does not depend on host-installed CLIs.
_CLI_STUB_NAMES = ("agy", "codewhale", "codex", "copilot")


@pytest.fixture(scope="session")
def cli_stubs_dir(tmp_path_factory):
    """Create directory with executable stub files for external CLIs."""
    stubs = tmp_path_factory.mktemp("cli_stubs")
    for name in _CLI_STUB_NAMES:
        p = stubs / name
        p.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
    return stubs


@pytest.fixture(autouse=True)
def stub_cli_bins(cli_stubs_dir, monkeypatch, request):
    """Point AI_ROUTER_<NAME>_BIN to host-independent stub executables.

    _cli_bin() honours AI_ROUTER_<NAME>_BIN first when it points to an
    existing executable file. Pointing each CLI to an executable stub ensures
    resolution succeeds deterministically on any host (e.g. CI runners without
    agy installed). The stub files are never executed because tests reaching
    process execution mock subprocess.run.
    """
    if "test_delegate_agent" in request.node.nodeid:
        return
    for name in _CLI_STUB_NAMES:
        monkeypatch.setenv(f"AI_ROUTER_{name.upper()}_BIN", str(cli_stubs_dir / name))
