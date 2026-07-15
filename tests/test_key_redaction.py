"""Security tests: the API key must never appear in URLs or in any error
message that leaves the MCP server (real leak 2026-07-15: a stale server
process forwarded an httpx HTTPStatusError whose message contained the full
gemini URL with ?key=...).

No network calls: _post_with_retry is monkeypatched.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "mcp"))

import delegate as d  # noqa: E402
import server  # noqa: E402


class _FakeResponse:
    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {}}


def test_gemini_key_in_header_not_url(monkeypatch):
    captured = {}

    def fake_post(model, url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse()

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["gemini"]
    d.call_gemini(spec, "SECRET-KEY-123", [{"role": "user", "content": "hi"}], "")
    assert "SECRET-KEY-123" not in captured["url"]
    assert "key=" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "SECRET-KEY-123"


def test_redact_scrubs_key_query_param():
    msg = ("HTTPStatusError: Server error '503 Service Unavailable' for url "
           "'https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=AIzaSyFAKELEAKEDKEY123'")
    out = server._redact(msg)
    assert "AIzaSyFAKELEAKEDKEY123" not in out
    assert "?key=<redacted>" in out
    assert "503 Service Unavailable" in out  # diagnostic value preserved


def test_redact_scrubs_env_key_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyENVLEAK456")
    out = server._redact("boom AIzaSyENVLEAK456 in message")
    assert "AIzaSyENVLEAK456" not in out
    assert "<GEMINI_API_KEY>" in out


def test_rpc_error_applies_redaction(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    resp = server._rpc_error(1, -32000, "failed for url '...?key=AIzaSyXYZ&alt=json'")
    assert "AIzaSyXYZ" not in resp["error"]["message"]
    assert "?key=<redacted>" in resp["error"]["message"]
