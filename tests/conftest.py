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
