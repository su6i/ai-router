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
    # call_gemini()/provider=="gemini" is deliberately kept in delegate.py
    # (owner decree 2026-07-27 removed the free-quota "gemini" MODELS entry,
    # not the plumbing) — register a throwaway MODELS entry so these
    # protocol/write tests can keep exercising it via a fake call_gemini.
    monkeypatch.setitem(d.MODELS, "test-gemini", {
        "api": "test-gemini-model", "provider": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta",
        "cin": 0.0, "cout": 0.0, "key": "GEMINI_API_KEY",
        "quota_channel": "test-free",
    })
    monkeypatch.setitem(d.ALIASES, "test-gemini", "test-gemini")
    yield


def fake_caller(responses):
    """Returns a fake call_gemini that pops one canned response text per call."""
    calls = {"n": 0}

    def _call(spec, key, history, system):
        text = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return (text, spec["api"], f"resp-{calls['n']}", 10, 5, 0, None)

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
    files, patches, summary = d.parse_worker_response(text)
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
    files, patches, summary = d.parse_worker_response(text)
    assert [p for p, _ in files] == ["src/foo.py", "tests/test_foo.py"]
    assert files[0][1] == "content a"
    assert files[1][1] == "content b"
    assert summary == "two files"


def test_parse_no_blocks_returns_empty():
    files, patches, summary = d.parse_worker_response("sorry, I can't help with that.")
    assert files == []
    assert patches == []
    assert summary is None


def test_parse_malformed_header_ignored():
    # Header missing the closing "===" never matches the sentinel regex.
    text = "===FILE: src/foo.py\ngarbage\n===END FILE===\n"
    files, patches, summary = d.parse_worker_response(text)
    assert files == []
    assert patches == []


def test_parse_missing_summary_returns_none():
    text = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n"
    files, patches, summary = d.parse_worker_response(text)
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
        "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
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
        "fix foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
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
        "fix foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
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
            "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
            verify_cmd="", retries=1, project_root=tmp_path)


def test_worker_delegate_no_allow_write_rejects_all(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    out = d.worker_delegate(
        "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="",
        verify_cmd="", retries=1, project_root=tmp_path)

    assert not (tmp_path / "src" / "foo.py").exists()
    assert "files written : (none)" in out
    assert "REJECTED: src/foo.py" in out


def test_worker_delegate_writes_audit_line(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    d.worker_delegate(
        "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="true", retries=1, project_root=tmp_path)

    lines = d.AUDIT.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["mode"] == "worker"
    assert rec["files_written"] == ["src/foo.py"]
    assert rec["verify_status"] == "PASS"
    assert rec["attempts"] == 1

def test_build_worker_prompt_ordering():
    task = "do the thing"
    file_specs = [("foo.py", "print('foo')"), ("bar.py", "print('bar')")]
    prompt = d.build_worker_prompt(task, file_specs)
    
    # Files must come before the task string
    idx_foo = prompt.find("===CURRENT FILE: foo.py===")
    idx_bar = prompt.find("===CURRENT FILE: bar.py===")
    idx_task = prompt.find("Task:")
    
    assert idx_foo != -1
    assert idx_bar != -1
    assert idx_task != -1
    
    assert idx_foo < idx_task
    assert idx_bar < idx_task
    assert idx_foo < idx_bar

def test_build_worker_prompt_channel_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "__file__", str(tmp_path / "src" / "delegate.py"))
    templates = tmp_path / "templates" / "system-prompts"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "deepseek.md").write_text("I am deepseek.")
    
    prompt = d.build_worker_prompt("task", [], "flash")
    assert prompt.startswith("I am deepseek.")

# ---- PATCH protocol: parse_worker_response -------------------------------

def test_parse_patch_block():
    text = (
        "===PATCH: src/foo.py===\n"
        "===OLD===\n"
        "def foo():\n"
        "    return 1\n"
        "===NEW===\n"
        "def foo():\n"
        "    return 2\n"
        "===END PATCH===\n"
        "===SUMMARY===\n"
        "bumped return value\n"
        "===END SUMMARY===\n"
    )
    files, patches, summary = d.parse_worker_response(text)
    assert files == []
    assert patches == [("src/foo.py", "def foo():\n    return 1", "def foo():\n    return 2")]
    assert summary == "bumped return value"


def test_parse_mixed_file_and_patch_blocks():
    text = (
        "===FILE: src/new.py===\n"
        "print('new file')\n"
        "===END FILE===\n"
        "===PATCH: src/existing.py===\n"
        "===OLD===\n"
        "old line\n"
        "===NEW===\n"
        "new line\n"
        "===END PATCH===\n"
    )
    files, patches, summary = d.parse_worker_response(text)
    assert files == [("src/new.py", "print('new file')")]
    assert patches == [("src/existing.py", "old line", "new line")]


# ---- PATCH protocol: _apply_patches ---------------------------------------

def test_apply_patches_exact_one_match_succeeds(tmp_path):
    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("def foo():\n    return 1\n")

    applied, rejected = d._apply_patches(
        [("src/foo.py", "return 1", "return 2")], tmp_path, ["src/**"])

    assert rejected == []
    assert [p for p, _, _ in applied] == ["src/foo.py"]
    # size and delta must both be ints: they are fed to _human_size() and to the
    # audit writer, which crashed on a preformatted string (regression 2026-07-27).
    rel, size, delta = applied[0]
    assert isinstance(size, int) and isinstance(delta, int)
    assert size == len(target.read_bytes())
    assert delta == 0  # "return 1" -> "return 2" is the same length
    assert target.read_text() == "def foo():\n    return 2\n"


def test_patch_result_is_renderable_by_summary_formatter(tmp_path):
    """Regression: _apply_patches returned a preformatted size string while
    _write_files returned an int, so the FIRST successful patch blew up in
    _human_size() with "'<' not supported between 'str' and 'int'". The unit
    test above passed because it never rendered the result."""
    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("def foo():\n    return 1\n")

    applied, _ = d._apply_patches(
        [("src/foo.py", "return 1", "return 22")], tmp_path, ["src/**"])

    out = d._format_worker_summary(
        [], [], "", "SKIPPED", 1, 1, 0.0, "did a thing", 1, 0.0,
        "agy", "", [], patched=applied)

    assert "files patched : src/foo.py" in out
    assert "+1b" in out
    assert "ALL BLOCKS REJECTED" not in out  # a patch-only run is a success


def test_apply_patches_zero_matches_rejected(tmp_path):
    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("def foo():\n    return 1\n")

    applied, rejected = d._apply_patches(
        [("src/foo.py", "this text does not exist", "whatever")], tmp_path, ["src/**"])

    assert applied == []
    assert len(rejected) == 1
    assert rejected[0][0] == "src/foo.py"
    assert "not found verbatim" in rejected[0][1]
    assert target.read_text() == "def foo():\n    return 1\n"  # untouched


def test_apply_patches_two_matches_rejected(tmp_path):
    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\nx = 1\n")

    applied, rejected = d._apply_patches(
        [("src/foo.py", "x = 1", "x = 2")], tmp_path, ["src/**"])

    assert applied == []
    assert len(rejected) == 1
    assert "ambiguous" in rejected[0][1]
    assert "2" in rejected[0][1]
    assert target.read_text() == "x = 1\nx = 1\n"  # untouched


def test_apply_patches_missing_target_rejected(tmp_path):
    applied, rejected = d._apply_patches(
        [("src/nope.py", "old", "new")], tmp_path, ["src/**"])
    assert applied == []
    assert len(rejected) == 1
    assert rejected[0][0] == "src/nope.py"


# ---- large-file guard: _write_files ---------------------------------------

def test_write_files_large_file_full_rewrite_rejected_then_allowed(tmp_path):
    target = tmp_path / "src" / "big.py"
    target.parent.mkdir(parents=True)
    big_content = "x = 1\n" * 3000  # well over LARGE_FILE_BYTES (12_000)
    target.write_text(big_content)
    assert target.stat().st_size >= d.LARGE_FILE_BYTES

    written, rejected = d._write_files(
        [("src/big.py", "small replacement\n")], tmp_path, ["src/**"])
    assert written == []
    assert len(rejected) == 1
    assert "large file" in rejected[0][1]
    assert target.read_text() == big_content  # untouched

    written2, rejected2 = d._write_files(
        [("src/big.py", "small replacement\n")], tmp_path, ["src/**"],
        allow_full_rewrite=True)
    assert rejected2 == []
    assert [p for p, _ in written2] == ["src/big.py"]
    assert target.read_text() == "small replacement\n"


def test_write_files_shrink_guard_rejects_then_allowed(tmp_path):
    target = tmp_path / "src" / "mid.py"
    target.parent.mkdir(parents=True)
    # 10KB file, under LARGE_FILE_BYTES but a >50% shrink must still be caught
    ten_kb_content = "y = 1\n" * 1700
    target.write_text(ten_kb_content)
    assert target.stat().st_size < d.LARGE_FILE_BYTES

    written, rejected = d._write_files(
        [("src/mid.py", "tiny\n")], tmp_path, ["src/**"])
    assert written == []
    assert len(rejected) == 1
    assert "shrink" in rejected[0][1]
    assert target.read_text() == ten_kb_content  # untouched

    written2, rejected2 = d._write_files(
        [("src/mid.py", "tiny\n")], tmp_path, ["src/**"], allow_full_rewrite=True)
    assert rejected2 == []
    assert target.read_text() == "tiny\n"


def test_write_files_small_file_rewrite_unaffected_by_guard(tmp_path):
    # A normal small-file edit (well under LARGE_FILE_BYTES, no big shrink)
    # must NOT be blocked by the new guard — regression check.
    target = tmp_path / "src" / "small.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")

    written, rejected = d._write_files(
        [("src/small.py", "x = 2\n")], tmp_path, ["src/**"])
    assert rejected == []
    assert [p for p, _ in written] == ["src/small.py"]
    assert target.read_text() == "x = 2\n"


# ---- call_agy_print (the agy worker backend) -------------------------------

def test_call_agy_print_success(monkeypatch, tmp_path):
    captured_cmd = {}

    class FakeCompleted:
        returncode = 0
        stdout = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n"
        stderr = ""

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured_cmd["cmd"] = cmd
        captured_cmd["cwd"] = cwd
        return FakeCompleted()

    monkeypatch.setattr(d.subprocess, "run", fake_run)

    content, echoed, rid, pin, pout, cache, cache_miss = d.call_agy_print(
        "do the task", "Gemini 3.1 Pro (High)", tmp_path, timeout_s=60)

    assert content == "===FILE: src/foo.py===\nx = 1\n===END FILE==="
    assert echoed == "Gemini 3.1 Pro (High)"
    assert pin == 0 and pout == 0 and cache == 0
    # Deliberate design: NO --add-dir on the worker path (the router's own
    # parse-and-write is the only writer; --mode plan is the other guard).
    assert "--add-dir" not in captured_cmd["cmd"]
    assert "--mode" in captured_cmd["cmd"]
    assert captured_cmd["cmd"][captured_cmd["cmd"].index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" in captured_cmd["cmd"]
    assert captured_cmd["cwd"] == str(tmp_path)


def test_call_agy_print_nonzero_exit_raises(monkeypatch, tmp_path):
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(d.subprocess, "run",
                        lambda *a, **k: FakeCompleted())

    with pytest.raises(d.ProviderError):
        d.call_agy_print("task", "Gemini 3.1 Pro (High)", tmp_path, timeout_s=60)


def test_call_agy_print_empty_stdout_raises(monkeypatch, tmp_path):
    class FakeCompleted:
        returncode = 0
        stdout = "   "
        stderr = ""

    monkeypatch.setattr(d.subprocess, "run",
                        lambda *a, **k: FakeCompleted())

    with pytest.raises(d.ProviderError):
        d.call_agy_print("task", "Gemini 3.1 Pro (High)", tmp_path, timeout_s=60)


def test_call_agy_print_timeout_raises(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        raise d.subprocess.TimeoutExpired(cmd="agy", timeout=60)

    monkeypatch.setattr(d.subprocess, "run", fake_run)

    with pytest.raises(d.ProviderError):
        d.call_agy_print("task", "Gemini 3.1 Pro (High)", tmp_path, timeout_s=60)


def test_worker_delegate_agy_model_needs_no_env_key(tmp_path, monkeypatch):
    # spec["key"] == "" for the agy model — worker_delegate must not sys.exit
    # demanding an env var that doesn't apply to a CLI-subscription channel.
    response = (
        "===FILE: src/foo.py===\n"
        "def foo():\n"
        "    return 1\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "added foo()\n"
        "===END SUMMARY===\n"
    )
    monkeypatch.setattr(d, "call_agy_print",
                        lambda prompt, model_name, project_root, timeout_s: (
                            response, model_name, None, 0, 0, 0, None))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    out = d.worker_delegate(
        "add foo()", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="", retries=1, project_root=tmp_path)

    assert (tmp_path / "src" / "foo.py").read_text() == "def foo():\n    return 1\n"
    assert "files written : src/foo.py" in out


def test_worker_delegate_agy_records_cost_unknown(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_agy_print",
                        lambda prompt, model_name, project_root, timeout_s: (
                            response, model_name, None, 0, 0, 0, None))

    d.worker_delegate(
        "add foo()", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="", retries=1, project_root=tmp_path)

    lines = d.AUDIT.read_text().strip().splitlines()
    rec = json.loads(lines[0])
    assert rec["cost_unknown"] is True
    assert rec["quota_channel"] == "google-ai-pro"


def test_worker_delegate_no_fallback_on_provider_error(tmp_path, monkeypatch):
    def raising_caller(*a, **k):
        raise d.ProviderError("test-gemini-model", 429, "quota exhausted")
    monkeypatch.setattr(d, "call_gemini", raising_caller)

    with pytest.raises(ValueError, match="No automatic paid fallback"):
        d.worker_delegate(
            "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
            verify_cmd="", retries=1, project_root=tmp_path)
