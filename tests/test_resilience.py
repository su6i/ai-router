import pytest
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from delegate import _post_with_retry, ProviderError, delegate, resolve_model

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test_gemini")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test_ds")
    monkeypatch.setenv("MINIMAX_API_KEY", "test_mm")
    # mock side effects
    monkeypatch.setattr("delegate.check_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr("delegate._write_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr("delegate.project_info", lambda: ("test_project", "abcdef"))

def test_retry_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda x: sleeps.append(x))
    
    responses = [
        httpx.Response(429, request=httpx.Request("POST", "http://test")),
        httpx.Response(500, request=httpx.Request("POST", "http://test")),
        httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "http://test")),
    ]
    calls = []
    
    def mock_post(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)
        
    monkeypatch.setattr("httpx.post", mock_post)
    
    r = _post_with_retry("test_model", "http://test")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [1, 3]

def test_retry_exhausted(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda x: sleeps.append(x))
    
    def mock_post(*args, **kwargs):
        return httpx.Response(500, request=httpx.Request("POST", "http://test"))
        
    monkeypatch.setattr("httpx.post", mock_post)
    
    with pytest.raises(ProviderError) as exc:
        _post_with_retry("test_model", "http://test")
        
    assert "test_model failed: HTTP 500" in str(exc.value)
    assert exc.value.status == 500
    assert sleeps == [1, 3]

def test_immediate_fail(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda x: sleeps.append(x))
    
    def mock_post(*args, **kwargs):
        return httpx.Response(400, request=httpx.Request("POST", "http://test"))
        
    monkeypatch.setattr("httpx.post", mock_post)
    
    with pytest.raises(ProviderError) as exc:
        _post_with_retry("test_model", "http://test")
        
    assert "test_model failed: HTTP 400" in str(exc.value)
    assert exc.value.status == 400
    assert sleeps == []

def test_minimax_failure_raises_no_fallback(monkeypatch):
    # Owner decree 2026-07-27: silent escalation from a failing channel to a
    # PAID one is banned outright. minimax failing with HTTP 402 must raise
    # loudly, and flash (DeepSeek) must never be called automatically.
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda x: sleeps.append(x))

    flash_called = []
    def mock_post(*args, **kwargs):
        if "api.minimax.io" in args[0]:
            return httpx.Response(402, request=httpx.Request("POST", args[0]))
        if "api.deepseek.com" in args[0]:
            flash_called.append(1)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "flash answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}
            }, request=httpx.Request("POST", args[0]))
        return httpx.Response(500, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr("httpx.post", mock_post)

    with pytest.raises(ValueError, match="No automatic paid fallback"):
        delegate("hello", "minimax", use_cache=False)

    assert len(flash_called) == 0, "paid flash must NEVER be called automatically"

def test_malformed_response(monkeypatch):
    def mock_post(*args, **kwargs):
        return httpx.Response(200, json={"broken": "yes"}, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr("httpx.post", mock_post)
    
    with pytest.raises(ValueError) as exc:
        delegate("hello", "flash", use_cache=False)
        
    assert "missing choices[0].message.content" in str(exc.value)
    assert "No automatic paid fallback" in str(exc.value)

def test_resolve_model_raises_value_error():
    with pytest.raises(ValueError, match="unknown model 'nope'"):
        resolve_model("nope")
