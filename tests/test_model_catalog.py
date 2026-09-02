import json
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

# Match existing tests/ sys.path conventions
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    import delegate  # noqa: E402
except ImportError:
    from src import delegate  # noqa: E402


_SAMPLE_AGY_MODELS_STDOUT = """Fetching available models...
gemini-3.7-flash-high\tGemini 3.7 Flash (High)
gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
gemini-3.6-flash-high\tGemini 3.6 Flash (High)
gemini-3.6-flash-medium\tGemini 3.6 Flash (Medium)
gemini-3.6-flash-low\tGemini 3.6 Flash (Low)
gemini-3.1-pro-high\tGemini 3.1 Pro (High)
gemini-3.1-pro-low\tGemini 3.1 Pro (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


def test_parse_agy_models_stdout():
    ids = set(delegate.parse_agy_models(_SAMPLE_AGY_MODELS_STDOUT))
    assert ids == {
        "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
        "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
        "gemini-3.1-pro-high", "gemini-3.1-pro-low",
        "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
    }
    assert "Fetching" not in "".join(ids)


def test_registry_keys_match_shape():
    valid_patterns = [
        r"minimax",
        r"flash",
        r"pro",
        r"grok",
        r"grok-4\.5",
        r"gemini-3\.\d+-(flash|pro)-(high|medium|low)",
        r"claude-[\w-]+",
        r"gpt-oss-[\w-]+",
    ]
    combined_pattern = re.compile("^(?:" + "|".join(valid_patterns) + ")$")

    mismatched = [k for k in delegate.MODELS if not combined_pattern.fullmatch(k)]
    assert not mismatched, f"Keys do not match expected shape: {mismatched}"


@pytest.mark.skipif(shutil.which("agy") is None, reason="agy CLI not installed/offline")
def test_live_channel_agy_models(monkeypatch):
    # resolve_model() registers a served-but-unregistered id, so keep that
    # mutation inside this test.
    monkeypatch.setattr(delegate, "MODELS", delegate.MODELS.copy())
    r = subprocess.run(["agy", "models"], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"agy models failed: {r.stderr}"
    served_agy = set(delegate.parse_agy_models(r.stdout))
    registered_agy = {k for k, v in delegate.MODELS.items() if v["provider"] == "agy_cli"}
    # Owner decree 2026-09-03 (see delegate.py): the static MODELS table is a
    # pricing/floor table, not the source of truth for "which generation is
    # current". A live id the static table predates is the DESIGNED state, not
    # drift -- register_agy_model() picks it up on first use -- so it is
    # neither asserted nor warned about here; warning on it would fire on every
    # release Google ships, which is noise that trains the eye to ignore it.
    # The real promise, and what stays a hard assertion, is the README's: every
    # id the channel prints is routable by that exact name. That catches what
    # the old served-subset-of-registry assert stood in for -- an id whose shape
    # resolve_model() cannot recognise (a new family, a renamed effort suffix).
    for mid in sorted(served_agy):
        assert delegate.resolve_model(mid) == mid, f"live-served id not routable by name: {mid}"
    extra = registered_agy - served_agy
    if extra:
        warnings.warn(f"MODELS registers agy ids the live CLI no longer serves (registry deletion is an architect decision, not asserted here): {sorted(extra)}")


def test_newest_wins_by_numeric_version(monkeypatch):
    monkeypatch.setattr(delegate, "MODELS", delegate.MODELS.copy())
    monkeypatch.setattr(delegate, "agy_served_models", lambda *a, **kw: ["gemini-3.9-flash-high", "gemini-3.10-flash-high"])
    assert delegate.latest_agy_model("flash", "high") == "gemini-3.10-flash-high"


def test_family_effort_selectivity(monkeypatch):
    monkeypatch.setattr(delegate, "MODELS", delegate.MODELS.copy())
    catalog = ["gemini-3.10-flash-high", "gemini-3.11-flash-low", "gemini-3.2-pro-high"]
    monkeypatch.setattr(delegate, "agy_served_models", lambda *a, **kw: catalog)
    assert delegate.latest_agy_model("flash", "low") == "gemini-3.11-flash-low"
    assert delegate.latest_agy_model("pro", "high") == "gemini-3.2-pro-high"


def test_register_agy_model(monkeypatch):
    monkeypatch.setattr(delegate, "MODELS", delegate.MODELS.copy())
    new_id = "gemini-9.9-flash-high"
    assert new_id not in delegate.MODELS

    returned_id = delegate.register_agy_model(new_id)
    assert returned_id == new_id
    assert new_id in delegate.MODELS
    spec = delegate.MODELS[new_id]
    assert spec["provider"] == "agy_cli"
    assert spec["cin"] == 0.0
    assert spec["cout"] == 0.0
    assert spec["quota_channel"] == delegate.agy_quota_channel(new_id)

    # Check idempotence
    spec_id = id(spec)
    delegate.register_agy_model(new_id)
    assert id(delegate.MODELS[new_id]) == spec_id


def test_agy_served_models_cache_behavior(monkeypatch, tmp_path):
    cache_file = tmp_path / "agy_models.json"
    monkeypatch.setattr(delegate, "AGY_CATALOG_CACHE", cache_file)

    # 1. Fresh cache: returns cached list without spawning subprocess
    cache_file.write_text(json.dumps({"fetched_at": time.time(), "models": ["cached-model"]}))
    def fail_if_called(*a, **kw):
        raise AssertionError("subprocess.run should not be called when cache is fresh")
    monkeypatch.setattr(subprocess, "run", fail_if_called)
    assert delegate.agy_served_models() == ["cached-model"]

    # 2. Stale cache: refetches and updates cache
    stale_time = time.time() - delegate.AGY_CATALOG_TTL_S - 10
    cache_file.write_text(json.dumps({"fetched_at": stale_time, "models": ["old-model"]}))

    class FakeResult:
        returncode = 0
        stdout = "gemini-3.9-flash-high\tFake Model"

    def fake_run(*a, **kw):
        return FakeResult()
    monkeypatch.setattr(subprocess, "run", fake_run)

    fresh_models = delegate.agy_served_models()
    assert fresh_models == ["gemini-3.9-flash-high"]
    new_cache = json.loads(cache_file.read_text())
    assert new_cache["models"] == ["gemini-3.9-flash-high"]
    assert new_cache["fetched_at"] > stale_time

    # 3. Subprocess fails: returns stale cache
    cache_file.write_text(json.dumps({"fetched_at": stale_time, "models": ["fallback-model"]}))
    def failing_run(*a, **kw):
        raise OSError("simulate subprocess failure")
    monkeypatch.setattr(subprocess, "run", failing_run)

    assert delegate.agy_served_models() == ["fallback-model"]


def test_latest_agy_model_fallback_to_static(monkeypatch):
    monkeypatch.setattr(delegate, "MODELS", delegate.MODELS.copy())
    monkeypatch.setattr(delegate, "agy_served_models", lambda *a, **kw: [])
    # Should fall back to static catalog (delegate.MODELS)
    # The newest static 'pro' 'high' is gemini-3.1-pro-high
    assert delegate.latest_agy_model("pro", "high") == "gemini-3.1-pro-high"


def test_resolve_model_live_and_missing(monkeypatch):
    monkeypatch.setattr(delegate, "MODELS", delegate.MODELS.copy())
    fake_catalog = ["gemini-3.9-flash-high"]
    monkeypatch.setattr(delegate, "agy_served_models", lambda *a, **kw: fake_catalog)

    # Resolves successfully and registers
    assert delegate.resolve_model("gemini-3.9-flash-high") == "gemini-3.9-flash-high"
    assert "gemini-3.9-flash-high" in delegate.MODELS

    # Fails for unknown model
    with pytest.raises(ValueError, match="unknown model"):
        delegate.resolve_model("gemini-3.10-flash-high")
