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

import pytest

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
