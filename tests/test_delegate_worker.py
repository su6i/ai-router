"""Tests for worker mode (--files) in delegate.py — WO1b, SPEC v1 in
DELEGATE-TOOL-DESIGN.md § "Worker-mode wire protocol".

No network calls: call_gemini is monkeypatched with canned sentinel-line
responses. CACHE/AUDIT/SESSIONS point at tmp_path so runs never touch the
real vault ledger.
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


def fake_caller(responses):
    """Returns a fake call_gemini that pops one canned response text per call."""
    calls = {"n": 0}

    def _call(spec, key, history, system):
        text = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return (text, spec["api"], f"resp-{calls['n']}", 10, 5, 0)

    _call.calls = calls
    return _call


# ---- parse_worker_response ---------------------------------------------------

def test_parse_happy_path_single_file():
    text = (
        "===FILE: src/foo.py===\n"
        "def foo():\n"
        "    return 1\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "added foo()\n"
        "===END SUMMARY===\n"
    )
    files, summary = d.parse_worker_response(text)
    assert files == [("src/foo.py", "def foo():\n    return 1")]
    assert summary == "added foo()"


def test_parse_multiple_files():
    text = (
        "===FILE: src/foo.py===\n"
        "content a\n"
        "===END FILE===\n"
        "===FILE: tests/test_foo.py===\n"
        "content b\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "two files\n"
        "===END SUMMARY===\n"
    )
    files, summary = d.parse_worker_response(text)
    assert [p for p, _ in files] == ["src/foo.py", "tests/test_foo.py"]
    assert files[0][1] == "content a"
    assert files[1][1] == "content b"
    assert summary == "two files"


def test_parse_no_blocks_returns_empty():
    files, summary = d.parse_worker_response("sorry, I can't help with that.")
    assert files == []
    assert summary is None


def test_parse_malformed_header_ignored():
    # Header missing the closing "===" never matches the sentinel regex.
    text = "===FILE: src/foo.py\ngarbage\n===END FILE===\n"
    files, summary = d.parse_worker_response(text)
    assert files == []


def test_parse_missing_summary_returns_none():
    text = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n"
    files, summary = d.parse_worker_response(text)
    assert files == [("src/foo.py", "x = 1")]
    assert summary is None


# ---- path safety --------------------------------------------------------------

def test_safe_write_path_rejects_absolute(tmp_path):
    path, err = d._safe_write_path("/etc/passwd", tmp_path, ["**"])
    assert path is None
    assert "absolute" in err


def test_safe_write_path_rejects_dotdot(tmp_path):
    path, err = d._safe_write_path("../outside.py", tmp_path, ["**"])
    assert path is None
    assert ".." in err


def test_safe_write_path_rejects_outside_allow_write(tmp_path):
    path, err = d._safe_write_path("other/file.py", tmp_path, ["src/**"])
    assert path is None
    assert "allow-write" in err


def test_safe_write_path_no_patterns_rejects_everything(tmp_path):
    path, err = d._safe_write_path("src/foo.py", tmp_path, [])
    assert path is None
    assert "no --allow-write" in err


def test_safe_write_path_accepts_matching_glob(tmp_path):
    path, err = d._safe_write_path("src/foo.py", tmp_path, ["src/**"])
    assert err is None
    assert path == (tmp_path / "src" / "foo.py").resolve()


# ---- _write_files: exact bytes -------------------------------------------------

def test_write_files_exact_bytes_and_adds_trailing_newline(tmp_path):
    files = [("src/a.py", "no newline at end"), ("src/b.py", "already has one\n")]
    written, rejected = d._write_files(files, tmp_path, ["src/**"])
    assert rejected == []
    assert (tmp_path / "src" / "a.py").read_text() == "no newline at end\n"
    assert (tmp_path / "src" / "b.py").read_text() == "already has one\n"
    assert {p for p, _ in written} == {"src/a.py", "src/b.py"}


def test_write_files_reports_rejected_without_aborting(tmp_path):
    files = [("src/ok.py", "fine\n"), ("/etc/passwd", "nope\n")]
    written, rejected = d._write_files(files, tmp_path, ["src/**"])
    assert [p for p, _ in written] == ["src/ok.py"]
    assert [p for p, _ in rejected] == ["/etc/passwd"]
    assert not (tmp_path / "etc" / "passwd").exists()


# ---- worker_delegate: verify pass / fail+retry / fail-final -------------------

def test_worker_delegate_verify_pass(tmp_path, monkeypatch):
    response = (
        "===FILE: src/foo.py===\n"
        "def foo():\n"
        "    return 1\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "added foo()\n"
        "===END SUMMARY===\n"
    )
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    out = d.worker_delegate(
        "add foo()", "gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="true", retries=1, project_root=tmp_path)

    assert (tmp_path / "src" / "foo.py").read_text() == "def foo():\n    return 1\n"
    assert "files written : src/foo.py" in out
    assert "verify        : true → PASS" in out
    assert "[attempt 1/2]" in out
    assert "added foo()" in out
    assert len(out.splitlines()) <= 25


def test_worker_delegate_verify_fail_then_retry_passes(tmp_path, monkeypatch):
    bad = (
        "===FILE: src/foo.py===\n"
        "broken\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "first try\n"
        "===END SUMMARY===\n"
    )
    good = (
        "===FILE: src/foo.py===\n"
        "fixed\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "fixed it\n"
        "===END SUMMARY===\n"
    )
    monkeypatch.setattr(d, "call_gemini", fake_caller([bad, good]))

    verify_calls = {"n": 0}

    def fake_verify(cmd, cwd):
        verify_calls["n"] += 1
        ok = verify_calls["n"] >= 2
        return ok, "" if ok else "1 failed", 0.1

    monkeypatch.setattr(d, "run_verify", fake_verify)

    out = d.worker_delegate(
        "fix foo()", "gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="uv run pytest -q", retries=1, project_root=tmp_path)

    assert (tmp_path / "src" / "foo.py").read_text() == "fixed\n"
    assert verify_calls["n"] == 2
    assert "verify        : uv run pytest -q → PASS" in out
    assert "[attempt 2/2]" in out
    assert "fixed it" in out
    assert len(out.splitlines()) <= 25


def test_worker_delegate_verify_fails_final_shows_tail(tmp_path, monkeypatch):
    response = (
        "===FILE: src/foo.py===\n"
        "still broken\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "could not fix\n"
        "===END SUMMARY===\n"
    )
    monkeypatch.setattr(d, "call_gemini", fake_caller([response, response]))
    monkeypatch.setattr(d, "run_verify",
                        lambda cmd, cwd: (False, "\n".join(f"line {i}" for i in range(20)), 0.1))

    out = d.worker_delegate(
        "fix foo()", "gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="uv run pytest -q", retries=1, project_root=tmp_path)

    assert "verify        : uv run pytest -q → FAIL" in out
    assert "[attempt 2/2]" in out
    assert "verify output (last 15 lines):" in out
    assert "line 19" in out and "line 4" not in out  # only last 15 lines kept
    assert len(out.splitlines()) <= 25


def test_worker_delegate_protocol_failure_reprompts_then_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "call_gemini", fake_caller(["no file blocks here", "still nothing"]))

    with pytest.raises(SystemExit):
        d.worker_delegate(
            "add foo()", "gemini", files_arg="src/foo.py", allow_write_arg="src/**",
            verify_cmd="", retries=1, project_root=tmp_path)


def test_worker_delegate_no_allow_write_rejects_all(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    out = d.worker_delegate(
        "add foo()", "gemini", files_arg="src/foo.py", allow_write_arg="",
        verify_cmd="", retries=1, project_root=tmp_path)

    assert not (tmp_path / "src" / "foo.py").exists()
    assert "files written : (none)" in out
    assert "REJECTED: src/foo.py" in out


def test_worker_delegate_writes_audit_line(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    d.worker_delegate(
        "add foo()", "gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="true", retries=1, project_root=tmp_path)

    lines = d.AUDIT.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["mode"] == "worker"
    assert rec["files_written"] == ["src/foo.py"]
    assert rec["verify_status"] == "PASS"
    assert rec["attempts"] == 1
