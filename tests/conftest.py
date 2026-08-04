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
