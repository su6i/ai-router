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


def test_agy_served_models_one_subprocess_per_process(monkeypatch, tmp_path):
    # T-944: two consecutive resolutions against a cold cache must shell out
    # to `agy models` exactly once -- the second call is served from the
    # in-process memo, not a second subprocess.
    cache_file = tmp_path / "agy_models.json"
    monkeypatch.setattr(delegate, "AGY_CATALOG_CACHE", cache_file)
    monkeypatch.setattr(delegate.agy_served_models, "_memo",
                         {"path": None, "disk_fetched_at": None, "resolved_fetched_at": None, "models": None},
                         raising=False)

    call_count = {"n": 0}

    class FakeResult:
        returncode = 0
        stdout = "gemini-3.9-flash-high\tFake Model\n"

    def counting_run(*a, **kw):
        call_count["n"] += 1
        return FakeResult()

    monkeypatch.setattr(subprocess, "run", counting_run)

    first = delegate.agy_served_models()
    second = delegate.agy_served_models()

    assert first == ["gemini-3.9-flash-high"]
    assert second == ["gemini-3.9-flash-high"]
    assert call_count["n"] == 1, f"expected exactly one ['agy', 'models'] subprocess call, got {call_count['n']}"


def test_agy_catalog_write_failure_falls_back_without_raising(monkeypatch, tmp_path):
    # T-944: a failed atomic write (temp file + os.replace) must never raise
    # and must never leave a corrupt/partial cache file or a stray temp file
    # behind -- resolution still succeeds off the freshly-fetched, in-memory
    # value even though persistence to disk failed.
    cache_file = tmp_path / "agy_models.json"
    monkeypatch.setattr(delegate, "AGY_CATALOG_CACHE", cache_file)
    monkeypatch.setattr(delegate.agy_served_models, "_memo",
                         {"path": None, "disk_fetched_at": None, "resolved_fetched_at": None, "models": None},
                         raising=False)

    class FakeResult:
        returncode = 0
        stdout = "gemini-3.9-flash-high\tFake Model\n"

    def fake_run(*a, **kw):
        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    def failing_replace(*a, **kw):
        raise OSError("simulated failure")

    monkeypatch.setattr(delegate.os, "replace", failing_replace)

    result = delegate.agy_served_models()

    assert result == ["gemini-3.9-flash-high"]
    assert not cache_file.exists(), "a failed os.replace must never leave a partial cache file in place"
    leftover_tmp = list(tmp_path.glob(".agy-catalog-*.tmp"))
    assert leftover_tmp == [], f"leftover temp file(s) after failed write: {leftover_tmp}"


def test_router_default_absent_creates_file_and_returns_defaults(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(delegate, "DATA_DIR", data_dir)
    defaults_file = data_dir / "router_defaults.json"
    assert not defaults_file.exists()

    for kind in ("worker", "agent", "research"):
        assert delegate.router_default(kind) == "gemini-flash"

    assert defaults_file.exists()
    content = json.loads(defaults_file.read_text())
    assert content == {
        "worker_model": "gemini-flash",
        "agent_model": "gemini-flash",
        "research_model": "gemini-flash",
    }


def test_router_default_corrupt_or_wrong_shape_falls_back(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(delegate, "DATA_DIR", data_dir)
    defaults_file = data_dir / "router_defaults.json"

    # Corrupt JSON
    defaults_file.write_text("{corrupt json")
    assert delegate.router_default("worker") == "gemini-flash"
    assert delegate.router_default("agent") == "gemini-flash"
    assert delegate.router_default("research") == "gemini-flash"

    # Wrong shape: array
    defaults_file.write_text("[]")
    assert delegate.router_default("worker") == "gemini-flash"

    # Wrong shape: non-dict primitive
    defaults_file.write_text('"invalid"')
    assert delegate.router_default("worker") == "gemini-flash"

    # Wrong value type
    defaults_file.write_text(json.dumps({"worker_model": 123}))
    assert delegate.router_default("worker") == "gemini-flash"

    # Missing key
    defaults_file.write_text(json.dumps({"other_key": "val"}))
    assert delegate.router_default("worker") == "gemini-flash"


def test_worker_mcp_and_agent_default_resolve_to_newest_flash(monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    import server

    newest_flash = delegate.latest_agy_model("flash", "high")
    assert "flash" in newest_flash

    # 1. Agent-mode default when runner == "agy" and model is None resolves to newest flash
    agent_default = delegate.resolve_model(delegate.router_default("agent"))
    assert agent_default == newest_flash

    # 2. Worker MCP handler when model argument is omitted resolves to newest flash
    worker_calls = []

    def fake_worker_delegate(prompt, model, *args, **kwargs):
        worker_calls.append(model)
        return "summary"

    monkeypatch.setattr(delegate, "worker_delegate", fake_worker_delegate)
    server.handle_delegate_worker({"prompt": "test prompt", "workdir": "/tmp"})
    assert worker_calls == [newest_flash]
