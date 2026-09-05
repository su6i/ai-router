"""Tests for T-954: mandatory verify command or explicit reason, and per-run file cap."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CACHE", tmp_path / "cache.db")
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(d, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(d, "WORKER_SESSIONS", tmp_path / "worker_sessions.json")
    monkeypatch.setattr(d, "BUDGETS", tmp_path / "budgets.json")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-tests")

    monkeypatch.setitem(d.MODELS, "test-gemini", {
        "api": "test-gemini-model", "provider": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta",
        "cin": 0.0, "cout": 0.0, "key": "GEMINI_API_KEY",
        "quota_channel": "test-free",
    })
    monkeypatch.setitem(d.ALIASES, "test-gemini", "test-gemini")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    yield bin_dir


def create_fake_bin(bin_dir, name, script_content):
    path = bin_dir / name
    path.write_text(script_content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def fake_caller(responses):
    calls = {"n": 0}

    def _call(spec, key, history, system):
        text = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return (text, spec["api"], f"resp-{calls['n']}", 10, 5, 0, None)

    _call.calls = calls
    return _call


def test_worker_abort_before_call_on_missing_verify_for_code_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        d, "call_gemini",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    with pytest.raises(ValueError):
        d.worker_delegate(
            "t", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
            verify_cmd="", retries=1, project_root=tmp_path,
        )


def test_worker_code_allow_write_requires_verify_even_with_docs_files_arg(tmp_path, monkeypatch):
    # files_arg alone looks docs-only, but allow_write_arg permits writing .py
    # files -- the gate must look at the UNION of both, not just files_arg,
    # since allow_write_arg is the actual write-permission boundary.
    monkeypatch.setattr(
        d, "call_gemini",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    with pytest.raises(ValueError):
        d.worker_delegate(
            "t", "test-gemini", files_arg="README.md", allow_write_arg="src/**/*.py",
            verify_cmd="", retries=1, project_root=tmp_path,
        )


def test_worker_docs_only_path_needs_no_verify(tmp_path, monkeypatch):
    response = "===FILE: docs/readme.md===\nhello\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))
    out = d.worker_delegate(
        "t", "test-gemini", files_arg="docs/readme.md", allow_write_arg="docs/**",
        verify_cmd="", retries=1, project_root=tmp_path,
    )
    assert not isinstance(out, Exception)
    assert (tmp_path / "docs" / "readme.md").read_text() == "hello\n"


def test_worker_escape_hatch_accepted_with_reason(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))
    d.worker_delegate(
        "t", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="", retries=1, project_root=tmp_path,
        no_verify_reason="deliberately skipping, this is a spike",
    )
    rec = json.loads(d.AUDIT.read_text().strip().splitlines()[-1])
    assert rec["no_verify_reason"] == "deliberately skipping, this is a spike"
    assert rec["verify_present"] is False


def test_worker_escape_hatch_rejected_when_blank(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="empty"):
        d.worker_delegate(
            "t", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
            verify_cmd="", retries=1, project_root=tmp_path,
            no_verify_reason="   ",
        )


def test_worker_file_cap_passes_at_cap_fails_at_cap_plus_one(tmp_path, monkeypatch):
    response = (
        "===FILE: a.md===\n1\n===END FILE===\n"
        "===FILE: b.md===\n2\n===END FILE===\n"
        "===SUMMARY===\nok\n===END SUMMARY===\n"
    )
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))
    d.worker_delegate(
        "t", "test-gemini", files_arg="a.md,b.md", allow_write_arg="**",
        verify_cmd="true", retries=1, project_root=tmp_path, max_files=2,
    )
    assert (tmp_path / "a.md").read_text() == "1\n"
    assert (tmp_path / "b.md").read_text() == "2\n"

    monkeypatch.setattr(
        d, "call_gemini",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    with pytest.raises(ValueError, match="cap"):
        d.worker_delegate(
            "t", "test-gemini", files_arg="a.md,b.md,c.md", allow_write_arg="**",
            verify_cmd="true", retries=1, project_root=tmp_path, max_files=2,
        )


def test_worker_max_files_env_var_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_MAX_FILES_PER_RUN", "1")
    with pytest.raises(ValueError, match="cap"):
        d.worker_delegate(
            "t", "test-gemini", files_arg="a.md,b.md", allow_write_arg="**",
            verify_cmd="true", retries=1, project_root=tmp_path,
        )


def test_worker_ledger_records_files_written_count(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))
    d.worker_delegate(
        "t", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="true", retries=1, project_root=tmp_path,
    )
    rec = json.loads(d.AUDIT.read_text().strip().splitlines()[-1])
    assert rec["files_written_count"] == 1
    assert rec["verify_present"] is True
    assert rec.get("no_verify_reason") is None


def test_agent_requires_verify_or_reason_for_any_run(tmp_path):
    with pytest.raises(ValueError):
        d.agent_delegate("task", runner="agy", workdir=tmp_path)


def test_agent_escape_hatch_accepted(isolated_paths, tmp_path):
    create_fake_bin(isolated_paths, "agy", "#!/usr/bin/env python3\nprint('ok')\n")
    out = d.agent_delegate(
        "task", runner="agy", workdir=tmp_path,
        no_verify_reason="spike, no tests yet",
    )
    assert "COMPLETED" in out
