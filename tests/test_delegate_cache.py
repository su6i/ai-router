"""Tests for the exact-hash cache in delegate.py (WO1 — DELEGATE-TOOL-DESIGN.md).

No network calls: call_openai/call_gemini are monkeypatched. CACHE and AUDIT
point at tmp_path so runs never touch the real vault ledger/cache.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CACHE", tmp_path / "cache.db")
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    monkeypatch.setattr(d, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    yield


def test_norm_collapses_whitespace():
    assert d._norm("  hi   there\n\n") == "hi there"
    assert d._norm(None) == ""


def test_cache_make_key_deterministic_and_sensitive():
    k1 = d.cache_make_key("gemini", "", "say hi")
    k2 = d.cache_make_key("gemini", "", "say   hi")   # whitespace-normalized -> same
    k3 = d.cache_make_key("gemini", "", "say hi ")     # trailing space -> same
    k4 = d.cache_make_key("flash", "", "say hi")       # different model -> different key
    k5 = d.cache_make_key("gemini", "sys", "say hi")   # different system -> different key
    assert k1 == k2 == k3
    assert k1 != k4
    assert k1 != k5
    assert len(k1) == 64  # sha256 hex digest


def test_cache_put_get_roundtrip():
    key = d.cache_make_key("gemini", "", "hello")
    assert d.cache_get(key) is None
    d.cache_put(key, "gemini", "hello", "world")
    assert d.cache_get(key) == "world"


def test_cache_get_increments_hits():
    key = d.cache_make_key("gemini", "", "hello")
    d.cache_put(key, "gemini", "hello", "world")
    d.cache_get(key)
    d.cache_get(key)
    con = d._cache_conn()
    hits = con.execute("SELECT hits FROM cache WHERE key=?", (key,)).fetchone()[0]
    con.close()
    assert hits == 2


def test_cache_get_fails_open_on_bad_db(monkeypatch):
    # CACHE path points at a directory instead of a file -> sqlite3.connect errors.
    bad_dir = d.CACHE
    bad_dir.mkdir(parents=True)
    assert d.cache_get("whatever-key") is None   # must not raise


def test_cache_put_fails_open_on_bad_db():
    bad_dir = d.CACHE
    bad_dir.mkdir(parents=True)
    d.cache_put("k", "gemini", "p", "r")   # must not raise


def test_delegate_cache_miss_then_hit(monkeypatch):
    calls = {"n": 0}

    def fake_call_gemini(spec, key, history, system, max_output_tokens=8192):
        calls["n"] += 1
        return ("cached answer", spec["api"], "resp-1", 10, 5, 0)

    monkeypatch.setattr(d, "call_gemini", fake_call_gemini)

    answer1 = d.delegate("say hi", "gemini")
    assert answer1 == "cached answer"
    assert calls["n"] == 1

    answer2 = d.delegate("say hi", "gemini")
    assert answer2 == "cached answer"
    assert calls["n"] == 1, "second identical call must be served from cache, not the provider"


def test_delegate_no_cache_flag_always_calls_provider(monkeypatch):
    calls = {"n": 0}

    def fake_call_gemini(spec, key, history, system, max_output_tokens=8192):
        calls["n"] += 1
        return ("answer", spec["api"], "resp", 1, 1, 0)

    monkeypatch.setattr(d, "call_gemini", fake_call_gemini)

    d.delegate("say hi", "gemini", use_cache=False)
    d.delegate("say hi", "gemini", use_cache=False)
    assert calls["n"] == 2, "--no-cache must bypass the cache on every call"


def test_delegate_session_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def fake_call_gemini(spec, key, history, system, max_output_tokens=8192):
        calls["n"] += 1
        return (f"answer {calls['n']}", spec["api"], "resp", 1, 1, 0)

    monkeypatch.setattr(d, "call_gemini", fake_call_gemini)

    d.delegate("say hi", "gemini", session="mysession")
    d.delegate("say hi", "gemini", session="mysession")
    assert calls["n"] == 2, "a --session conversation must never be served from the exact-hash cache"


def test_delegate_writes_audit_line_with_cached_flag(monkeypatch):
    def fake_call_gemini(spec, key, history, system, max_output_tokens=8192):
        return ("answer", spec["api"], "resp", 1, 1, 0)

    monkeypatch.setattr(d, "call_gemini", fake_call_gemini)

    d.delegate("say hi", "gemini")   # miss -> cached: false
    d.delegate("say hi", "gemini")   # hit  -> cached: true

    lines = d.AUDIT.read_text().strip().splitlines()
    assert len(lines) == 2
    rec1, rec2 = (json.loads(line) for line in lines)
    assert rec1["cached"] is False
    assert rec2["cached"] is True
