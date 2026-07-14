import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import httpx
import subprocess
import unicodedata
from delegate import (
    call_openai, ProviderError, main, show_audit, project_info, cache_make_key
)

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test_ds")
    monkeypatch.setenv("MINIMAX_API_KEY", "test_mm")

def test_call_openai_success(monkeypatch):
    def mock_post(*args, **kwargs):
        return httpx.Response(200, json={
            "id": "chatcmpl-123",
            "model": "deepseek-chat",
            "choices": [{"message": {"content": "hello world"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 5}
            }
        }, request=httpx.Request("POST", "http://test"))
    
    monkeypatch.setattr("httpx.post", mock_post)
    spec = {"api": "deepseek-chat", "url": "http://test"}
    ans, raw_model, id_, p_tok, c_tok, cache_tok = call_openai(spec, "Bearer xyz", [{"role": "user", "content": "hello"}], "")
    assert ans == "hello world"
    assert raw_model == "deepseek-chat"
    assert id_ == "chatcmpl-123"
    assert (p_tok, c_tok, cache_tok) == (10, 20, 5)

def test_call_openai_error_path(monkeypatch):
    def mock_post(*args, **kwargs):
        return httpx.Response(400, request=httpx.Request("POST", "http://test"))
    
    monkeypatch.setattr("httpx.post", mock_post)
    spec = {"api": "deepseek-chat", "url": "http://test"}
    with pytest.raises(ProviderError) as exc:
        call_openai(spec, "Bearer xyz", [{"role": "user", "content": "hello"}], "")
    assert exc.value.status == 400

def test_minimax_fallback_end_to_end(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("delegate.check_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr("delegate._write_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr("delegate.CACHE", tmp_path / "cache.db")
    
    flash_called = []
    def mock_post(*args, **kwargs):
        if "minimax" in args[0]:
            return httpx.Response(402, request=httpx.Request("POST", args[0]))
        flash_called.append(1)
        return httpx.Response(200, json={
            "id": "123", "model": "ds",
            "choices": [{"message": {"content": "flash fallback"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}
        }, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr("httpx.post", mock_post)
    
    test_args = ["delegate.py", "--model", "minimax", "-p", "hello", "--no-cache"]
    monkeypatch.setattr("sys.argv", test_args)
    
    main()
    assert len(flash_called) == 1
    
    captured = capsys.readouterr()
    assert "MiniMax failed (HTTP 402)" in captured.err

def test_show_audit(tmp_path, capsys, monkeypatch):
    audit_file = tmp_path / "audit.log"
    monkeypatch.setattr("delegate.AUDIT", audit_file)
    
    # Without audit
    show_audit()
    assert "no audit.log" in capsys.readouterr().out.lower()
    
    # With audit
    audit_file.write_text('{"ts": "2026", "model": "flash", "project": "ai-router", "cost_usd": 0.001}\n')
    show_audit()
    assert "flash" in capsys.readouterr().out

def test_project_info(monkeypatch):
    # Success
    def mock_run_success(*args, **kwargs):
        if "config" in args[0]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/foo/project_name.git\n", stderr="")
        if "rev-parse" in args[0]:
            return subprocess.CompletedProcess(args, 0, stdout="1234567\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr("subprocess.run", mock_run_success)
    proj, rev = project_info()
    assert proj == "project_name"
    assert rev == "1234567"
    
    # Return code != 0
    def mock_run_fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal")
    monkeypatch.setattr("subprocess.run", mock_run_fail)
    proj, rev = project_info()
    assert proj == "ai-router"
    assert rev is None
    
    # Binary missing
    def mock_run_missing(*args, **kwargs):
        raise FileNotFoundError("git not found")
    monkeypatch.setattr("subprocess.run", mock_run_missing)
    proj, rev = project_info()
    assert proj == "ai-router"
    assert rev is None

def test_cache_key():
    # Same prompt, different model
    key1 = cache_make_key("flash", "sys", "prompt", 8192)
    key2 = cache_make_key("pro", "sys", "prompt", 8192)
    assert key1 != key2
    
    # Different max_output_tokens -> different key
    key3 = cache_make_key("flash", "sys", "prompt", 4000)
    assert key1 != key3
    
    # NFC vs NFD -> same key now
    nfc = "é"
    nfd = "e\u0301"
    assert nfc != nfd
    assert unicodedata.normalize("NFC", nfc) == unicodedata.normalize("NFC", nfd)
    
    key_nfc = cache_make_key("flash", "", nfc, 8192)
    key_nfd = cache_make_key("flash", "", nfd, 8192)
    assert key_nfc == key_nfd

def test_cache_prune(monkeypatch, tmp_path):
    import datetime as dt
    db_file = tmp_path / "cache.db"
    monkeypatch.setattr("delegate.CACHE", db_file)
    monkeypatch.setattr("delegate.CACHE_MAX_ROWS", 5)
    monkeypatch.setattr("delegate.CACHE_MAX_AGE_DAYS", 90)
    
    from delegate import cache_put, cache_prune, _cache_conn
    
    # Insert 10 rows (cap is 5)
    # Give them staggered dates: some older than 90 days, some recent
    con = _cache_conn()
    now = dt.datetime.now().astimezone()
    for i in range(10):
        if i < 2:
            # older than 90 days
            created = (now - dt.timedelta(days=100)).isoformat()
        else:
            # recent, staggered
            created = (now - dt.timedelta(days=10 - i)).isoformat()
            
        con.execute("INSERT INTO cache VALUES(?,?,?,?,?,0)",
                    (f"key{i}", "flash", f"prompt{i}", "resp", created))
    con.commit()
    con.close()
    
    before, after = cache_prune()
    assert before == 10
    assert after == 5
    
    # Oldest by date should be gone (keys 0, 1 are > 90 days; keys 2, 3, 4 are the oldest of the remaining)
    con = _cache_conn()
    remaining = [r[0] for r in con.execute("SELECT key FROM cache ORDER BY created ASC").fetchall()]
    con.close()
    assert remaining == ["key5", "key6", "key7", "key8", "key9"]
    
    # Prune failure (corrupt DB) is silent
    db_file.write_bytes(b"not a database")
    # Shouldn't raise
    cache_put("new_key", "flash", "prompt", "resp")
    
    # Explicit cache_prune should return -1, -1 on failure
    assert cache_prune() == (-1, -1)
