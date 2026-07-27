"""Tests for xAI /v1/responses live search (WO-ai-router-0024).

grok's old chat/completions live-search request field is 410 Gone as of
2026-07; grok now routes through /v1/responses with a server-side
web_search tool. Fixtures below are the REAL response shape (not invented),
captured from a live probe by the architect on 2026-07-27 — see the WO for
the raw table this FIXTURE_RESPONSE is copied from (grok-4.5 row: pin=3497,
cached=2048, out=120, web_search_calls=1, ticks=92,324,000).

No network calls: httpx.post / _post_with_retry is monkeypatched, never
called for real.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


FIXTURE_RESPONSE = {
    "id": "resp_live_wo0024",
    "model": "grok-4.5",
    "status": "completed",
    "incomplete_details": None,
    "error": None,
    "output": [
        {"type": "reasoning", "id": "rs_1", "content": []},
        {"type": "web_search_call", "id": "ws_1", "status": "completed"},
        {
            "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "The answer is 42.",
                    "annotations": [
                        {"type": "url_citation", "url": "https://example.com", "title": "Example"}
                    ],
                }
            ],
        },
    ],
    "usage": {
        "input_tokens": 3497,
        "input_tokens_details": {"cached_tokens": 2048},
        "output_tokens": 120,
        "output_tokens_details": {"reasoning_tokens": 40},
        "total_tokens": 3617,
        "num_sources_used": 1,
        "cost_in_usd_ticks": 92_324_000,
        "server_side_tool_usage_details": {"web_search_calls": 1, "x_search_calls": 0},
    },
}


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CACHE", tmp_path / "cache.db")
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    monkeypatch.setattr(d, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setenv("GROK_API_KEY", "test-grok-key-xyz")
    monkeypatch.setattr(d, "check_budget", lambda *a, **k: None)
    monkeypatch.setattr(d, "project_info", lambda: ("test_project", "abcdef"))
    d._LAST_TRUE_COST = None
    yield
    d._LAST_TRUE_COST = None


def test_web_search_tool_and_cap_present_when_search_on(monkeypatch):
    captured = {}

    def fake_post(model, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return httpx.Response(200, json=FIXTURE_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["grok-4.5"]
    d.call_xai_responses(spec, "test-grok-key-xyz", [{"role": "user", "content": "hi"}], "")

    assert captured["url"].endswith("/responses")
    assert captured["json"]["tools"] == [{"type": "web_search"}]
    assert captured["json"]["max_tool_calls"] == d.XAI_MAX_TOOL_CALLS
    assert captured["headers"]["Authorization"] == "Bearer test-grok-key-xyz"


def test_no_tools_key_when_search_off(monkeypatch):
    captured = {}

    def fake_post(model, url, **kwargs):
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, json=FIXTURE_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["grok"]
    d.call_xai_responses(spec, "test-grok-key-xyz", [{"role": "user", "content": "hi"}], "",
                          web_search=False)

    assert "tools" not in captured["json"]
    assert "max_tool_calls" not in captured["json"]


def test_custom_max_tool_calls_sent(monkeypatch):
    captured = {}

    def fake_post(model, url, **kwargs):
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, json=FIXTURE_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["grok"]
    d.call_xai_responses(spec, "test-grok-key-xyz", [{"role": "user", "content": "hi"}], "",
                          max_tool_calls=3)

    assert captured["json"]["max_tool_calls"] == 3


def test_text_extracted_from_output_skipping_reasoning_and_web_search_call(monkeypatch):
    def fake_post(model, url, **kwargs):
        return httpx.Response(200, json=FIXTURE_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["grok-4.5"]
    text, model, rid, pin, pout, cached, cache_miss = d.call_xai_responses(
        spec, "test-grok-key-xyz", [{"role": "user", "content": "hi"}], "")

    assert text == "The answer is 42."
    assert model == "grok-4.5"
    assert rid == "resp_live_wo0024"
    assert pin == 3497
    assert pout == 120
    assert cached == 2048
    assert cache_miss is None


def test_incomplete_status_raises_provider_error(monkeypatch):
    incomplete = dict(FIXTURE_RESPONSE)
    incomplete["status"] = "incomplete"
    incomplete["incomplete_details"] = {"reason": "max_output_tokens"}

    def fake_post(model, url, **kwargs):
        return httpx.Response(200, json=incomplete, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["grok"]
    with pytest.raises(d.ProviderError) as exc:
        d.call_xai_responses(spec, "test-grok-key-xyz", [{"role": "user", "content": "hi"}], "")

    assert "incomplete" in str(exc.value)
    assert "max_output_tokens" in str(exc.value)


def test_malformed_response_no_output_text_raises(monkeypatch):
    broken = {"id": "x", "model": "grok-4.3", "status": "completed",
              "output": [{"type": "reasoning", "id": "rs_1"}], "usage": {}}

    def fake_post(model, url, **kwargs):
        return httpx.Response(200, json=broken, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)
    spec = d.MODELS["grok"]
    with pytest.raises(d.ProviderError) as exc:
        d.call_xai_responses(spec, "test-grok-key-xyz", [{"role": "user", "content": "hi"}], "")

    assert "no output_text" in str(exc.value)


def test_true_cost_from_ticks_overrides_token_table(monkeypatch):
    def fake_post(model, url, **kwargs):
        return httpx.Response(200, json=FIXTURE_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(d, "_post_with_retry", fake_post)

    audited = {}
    real_write_audit = d._write_audit

    def spy_write_audit(*args, **kwargs):
        audited.update(kwargs)
        audited["positional"] = args
        return real_write_audit(*args, **kwargs)

    monkeypatch.setattr(d, "_write_audit", spy_write_audit)

    d.delegate("what is 2+2?", "grok-4.5", use_cache=False)

    # ticks / 1e10 = 92_324_000 / 1e10 = 0.0092324 — the true billed cost.
    expected_true_cost = 92_324_000 / 1e10
    assert audited["positional"][9] == pytest.approx(expected_true_cost)
    assert audited["web_search_calls"] == 1

    # The token-table estimate for this usage would be a DIFFERENT (lower)
    # number — proving the true cost, not the table, won.
    spec = d.MODELS["grok-4.5"]
    table_cost = d.compute_token_cost(spec, 3497, 120, 2048)
    assert audited["positional"][9] != pytest.approx(table_cost)


def test_long_context_uses_cin_long_cout_long():
    spec = d.MODELS["grok-4.5"]
    pin, pout, cached = 250_000, 1000, 0
    cost = d.compute_token_cost(spec, pin, pout, cached)
    expected = pin / 1e6 * spec["cin_long"] + pout / 1e6 * spec["cout_long"]
    assert cost == pytest.approx(expected)
    # Sanity: this must be more expensive than the (wrong) short-context rate.
    short_rate_cost = pin / 1e6 * spec["cin"] + pout / 1e6 * spec["cout"]
    assert cost > short_rate_cost


def test_resolve_model_grok_4_5_hits_correct_price_row():
    resolved = d.resolve_model("grok-4.5")
    assert resolved == "grok-4.5"
    spec = d.MODELS[resolved]
    assert spec["cin"] == 2.00
    assert spec["cout"] == 6.00
    for alias in ("grok45", "grok4.5", "grok-4.5"):
        assert d.resolve_model(alias) == "grok-4.5"


def test_resolve_model_grok_default_stays_4_3():
    # A live 5-question A/B (WO-ai-router-0024 addendum, 2026-07-27) found
    # grok-4.3 5/5 correct at $0.172 total vs grok-4.5 4/5 at $0.618 total —
    # 3.6x the cost for worse quality. "grok" must keep resolving to 4.3.
    resolved = d.resolve_model("grok")
    assert resolved == "grok"
    assert d.MODELS[resolved]["api"] == "grok-4.3"


def test_api_key_never_leaks_in_provider_error(monkeypatch):
    secret_key = "xai-SUPER-SECRET-KEY-DO-NOT-LEAK"
    monkeypatch.setenv("GROK_API_KEY", secret_key)

    def fake_post_with_retry(model, url, **kwargs):
        # Simulate a network error whose raw text happens to echo the URL
        # (the historical leak vector, see test_key_redaction.py).
        raise d.ProviderError(model, "NETWORK_ERROR",
                               f"connect failed for {url}?key={secret_key}")

    monkeypatch.setattr(d, "_post_with_retry", fake_post_with_retry)
    spec = d.MODELS["grok"]
    with pytest.raises(d.ProviderError) as exc:
        d.call_xai_responses(spec, secret_key, [{"role": "user", "content": "hi"}], "")

    assert secret_key not in str(exc.value)


def test_cli_no_search_flag_disables_web_search(monkeypatch):
    """--no-search must reach call_xai_responses as web_search=False."""
    seen = {}

    def fake_call_xai_responses(spec, key, history, system, max_output_tokens=8192,
                                 web_search=True, max_tool_calls=d.XAI_MAX_TOOL_CALLS):
        seen["web_search"] = web_search
        seen["max_tool_calls"] = max_tool_calls
        return ("ok", spec["api"], "rid-1", 10, 5, 0, None)

    monkeypatch.setattr(d, "call_xai_responses", fake_call_xai_responses)
    monkeypatch.setattr(sys, "argv", ["delegate.py", "--model", "grok", "--no-search",
                                       "-p", "anything", "--quiet"])
    monkeypatch.setattr(d, "load_env", lambda: None)
    d.main()

    assert seen["web_search"] is False


def test_cli_max_tool_calls_override(monkeypatch):
    seen = {}

    def fake_call_xai_responses(spec, key, history, system, max_output_tokens=8192,
                                 web_search=True, max_tool_calls=d.XAI_MAX_TOOL_CALLS):
        seen["max_tool_calls"] = max_tool_calls
        return ("ok", spec["api"], "rid-1", 10, 5, 0, None)

    monkeypatch.setattr(d, "call_xai_responses", fake_call_xai_responses)
    monkeypatch.setattr(sys, "argv", ["delegate.py", "--model", "grok", "--max-tool-calls", "3",
                                       "-p", "anything", "--quiet"])
    monkeypatch.setattr(d, "load_env", lambda: None)
    d.main()

    assert seen["max_tool_calls"] == 3
