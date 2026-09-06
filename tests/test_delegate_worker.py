"""Tests for worker mode (--files) in delegate.py — WO1b, SPEC v1 in
DELEGATE-TOOL-DESIGN.md § "Worker-mode wire protocol".

No network calls: call_gemini is monkeypatched with canned sentinel-line
responses. CACHE/AUDIT/SESSIONS point at tmp_path so runs never touch the
real vault ledger.
"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CACHE", tmp_path / "cache.db")
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    monkeypatch.setattr(d, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(d, "WORKER_SESSIONS", tmp_path / "worker_sessions.json")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    # call_gemini()/provider=="gemini" is deliberately kept in delegate.py
    # (the free-quota "gemini" MODELS entry was removed,
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


class FakeAgyPopen:
    """Stand-in for subprocess.Popen(..., stdout=PIPE, stderr=PIPE, text=True)
    against the real agy --output-format stream-json event shapes. `lines` is
    a list of already-formatted NDJSON strings (each WITHOUT a trailing
    newline; this class adds it). Pass `hang=True` with an empty/short
    `lines` list to simulate a process that never produces the result event
    (used to exercise the watchdog timeout path) — `.kill()` releases it.
    """
    def __init__(self, lines, returncode=0, hang=False, stderr=""):
        self.args = ("agy",)
        self._lines = list(lines)
        self.returncode = returncode
        self._hang = hang
        self._killed = threading.Event()
        self.stderr = FakeStderr(stderr)
        self.stdout = self._stdout_iter()
        self.was_killed = False

    def _stdout_iter(self):
        for line in self._lines:
            yield line + "\n"
        if self._hang:
            self._killed.wait()

    def poll(self):
        if self._hang and not self._killed.is_set():
            return None
        return self.returncode

    def kill(self):
        self.was_killed = True
        self._killed.set()
        if self._hang:
            self.returncode = -9

    def terminate(self):
        self.kill()

    def wait(self, timeout=None):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def communicate(self, input=None, timeout=None):
        out = "".join(self.stdout)
        err = self.stderr.read()
        return (out, err)


class FakeStderr:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def agy_result_lines(response, usage, conversation_id, num_turns, duration_seconds, status="SUCCESS"):
    """Build the minimal two-line NDJSON stream (one agent_response step_update
    carrying `usage`, then the final result event) that a real single-turn
    `agy --output-format stream-json` call produces — matches the live
    experiment evidence exactly (T-960)."""
    step = json.dumps({"event": "step_update", "step_update": {
        "step_index": 1, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": duration_seconds, "usage": usage}})
    result = json.dumps({"event": "result", "result": {
        "conversation_id": conversation_id, "status": status, "response": response,
        "duration_seconds": duration_seconds, "num_turns": num_turns, "usage": usage}})
    return [step, result]


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
        return ok, "" if ok else "1 failed", 0.1, 0 if ok else 1

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
                        lambda cmd, cwd: (False, "\n".join(f"line {i}" for i in range(20)), 0.1, 1))

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
            verify_cmd="true", retries=1, project_root=tmp_path)


def test_worker_delegate_no_allow_write_rejects_all(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    out = d.worker_delegate(
        "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="",
        verify_cmd="true", retries=1, project_root=tmp_path)

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

    def fake_popen(cmd, cwd=None, stdout=None, stderr=None, text=None, env=None):
        captured_cmd["cmd"] = cmd
        captured_cmd["cwd"] = cwd
        captured_cmd["env"] = env
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10, "total_tokens": 160},
            "conv-123", 1, 2.5
        )
        return FakeAgyPopen(lines)

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)

    content, echoed, rid, pin, pout, cache, cache_miss = d.call_agy_print(
        "do the task", "gemini-3.1-pro-high", tmp_path, timeout_s=60)
    assert content == "===FILE: src/foo.py===\nx = 1\n===END FILE==="
    assert echoed == "gemini-3.1-pro-high"
    assert rid == "conv-123"
    assert pin == 100 and pout == 50 and cache == 10
    assert d._LAST_AGY_NUM_TURNS == 1
    assert d._LAST_AGY_DURATION_S == 2.5
    # The model id goes through verbatim and --effort is never sent: agy bakes
    # the effort level into the id and rejects the flag for the Claude ids.
    assert captured_cmd["cmd"][captured_cmd["cmd"].index("--model") + 1] == "gemini-3.1-pro-high"
    assert "--effort" not in captured_cmd["cmd"]
    # Deliberate design: NO --add-dir on the worker path (the router's own
    # parse-and-write is the only writer; --mode plan is the other guard).
    assert "--add-dir" not in captured_cmd["cmd"]
    assert "--mode" in captured_cmd["cmd"]
    assert captured_cmd["cmd"][captured_cmd["cmd"].index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" in captured_cmd["cmd"]
    # T-949: every worker subprocess must carry a marker the ROUTER sets one
    # process up, so the delegated model (or any shell command it runs) can
    # never spoof its way out of being detected as a worker session — this is
    # what makes `--score` refusing to run inside a worker session possible.
    assert captured_cmd["env"] is not None
    assert captured_cmd["env"].get("AI_ROUTER_IN_WORKER") == "1"
    assert "PATH" in captured_cmd["env"]  # a real copy of the parent env, not a bare dict
    assert "--output-format" in captured_cmd["cmd"]
    assert captured_cmd["cmd"][captured_cmd["cmd"].index("--output-format") + 1] == "stream-json"
    assert captured_cmd["cwd"] == str(tmp_path)


def test_call_agy_print_bad_json_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **k: FakeAgyPopen(["not json at all"]))

    with pytest.raises(d.ProviderError, match="BAD_JSON"):
        d.call_agy_print("task", "gemini-3.1-pro-high", tmp_path, timeout_s=60)


def test_call_agy_print_failure_status_raises(monkeypatch, tmp_path):
    lines = agy_result_lines("nope", {}, None, None, None, status="FAILURE")
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **k: FakeAgyPopen(lines))

    with pytest.raises(d.ProviderError, match="BAD_JSON"):
        d.call_agy_print("task", "gemini-3.1-pro-high", tmp_path, timeout_s=60)


def test_call_agy_print_nonzero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(d.subprocess, "Popen",
                        lambda *a, **k: FakeAgyPopen([], returncode=1, stderr="boom"))

    with pytest.raises(d.ProviderError):
        d.call_agy_print("task", "gemini-3.1-pro-high", tmp_path, timeout_s=60)


def test_call_agy_print_empty_stdout_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(d.subprocess, "Popen",
                        lambda *a, **k: FakeAgyPopen([], returncode=0))

    with pytest.raises(d.ProviderError):
        d.call_agy_print("task", "gemini-3.1-pro-high", tmp_path, timeout_s=60)


def test_call_agy_print_timeout_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **k: FakeAgyPopen([], hang=True))

    with pytest.raises(d.ProviderError, match="TIMEOUT"):
        d.call_agy_print("task", "gemini-3.1-pro-high", tmp_path, timeout_s=0.05)


def test_call_agy_print_budget_exceeded_kills_run(monkeypatch, tmp_path):
    steps = [
        json.dumps({"event": "step_update", "step_update": {
            "step_index": 1, "state": "DONE", "step_type": "agent_response",
            "duration_seconds": 1.0, "usage": {"total_tokens": 5000}}}),
        json.dumps({"event": "step_update", "step_update": {
            "step_index": 2, "state": "DONE", "step_type": "agent_response",
            "duration_seconds": 1.0, "usage": {"total_tokens": 5000}}}),
        json.dumps({"event": "step_update", "step_update": {
            "step_index": 3, "state": "DONE", "step_type": "agent_response",
            "duration_seconds": 1.0, "usage": {"total_tokens": 5000}}}),
    ]
    fake_proc = FakeAgyPopen(steps)
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **k: fake_proc)

    with pytest.raises(d.ProviderError, match="BUDGET_EXCEEDED"):
        d.call_agy_print("task", "gemini-3.1-pro-high", tmp_path, timeout_s=60, max_tokens_per_run=8000)

    assert fake_proc.was_killed


def test_call_agy_print_budget_zero_disables_breaker(monkeypatch, tmp_path):
    steps = [
        json.dumps({"event": "step_update", "step_update": {
            "step_index": 1, "state": "DONE", "step_type": "agent_response",
            "duration_seconds": 1.0, "usage": {"total_tokens": 50000}}}),
        json.dumps({"event": "step_update", "step_update": {
            "step_index": 2, "state": "DONE", "step_type": "agent_response",
            "duration_seconds": 1.0, "usage": {"total_tokens": 50000}}}),
        json.dumps({"event": "result", "result": {
            "conversation_id": "conv-zero", "status": "SUCCESS", "response": "===FILE: src/a.py===\nx=1\n===END FILE===",
            "duration_seconds": 2.0, "num_turns": 2, "usage": {"input_tokens": 90000, "output_tokens": 10000, "cache_read_tokens": 0, "total_tokens": 100000}}}),
    ]
    fake_proc = FakeAgyPopen(steps)
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **k: fake_proc)

    content, model, conv_id, pin, pout, cache, _ = d.call_agy_print(
        "task", "gemini-3.1-pro-high", tmp_path, timeout_s=60, max_tokens_per_run=0)
    assert not fake_proc.was_killed
    assert conv_id == "conv-zero"


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
                        lambda prompt, model_name, project_root, timeout_s, conversation_id=None: (
                            response, model_name, "conv-123", 100, 50, 0, None))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    out = d.worker_delegate(
        "add foo()", "gemini-3.1-pro-high", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="true", retries=1, project_root=tmp_path)

    assert (tmp_path / "src" / "foo.py").read_text() == "def foo():\n    return 1\n"
    assert "files written : src/foo.py" in out


def test_worker_delegate_agy_records_cost_unknown_false(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_agy_print",
                        lambda prompt, model_name, project_root, timeout_s, conversation_id=None: (
                            response, model_name, "conv-123", 100, 50, 0, None))

    d.worker_delegate(
        "add foo()", "gemini-3.1-pro-high", files_arg="src/foo.py", allow_write_arg="src/**",
        verify_cmd="true", retries=1, project_root=tmp_path)

    lines = d.AUDIT.read_text().strip().splitlines()
    rec = json.loads(lines[0])
    # cost_unknown is no longer set: --output-format json exposes real token
    # counts, so the ledger row stops lying about usage (cost stays $0).
    assert rec.get("cost_unknown") is not True
    # The pool is recorded per model, not as one flat "google-ai-pro": Gemini
    # and Claude are independent $0 pools inside the one subscription, and
    # spreading load across them is the whole point of the open catalog.
    assert rec["quota_channel"] == "google-ai-pro-gemini"


def test_worker_delegate_no_fallback_on_provider_error(tmp_path, monkeypatch):
    def raising_caller(*a, **k):
        raise d.ProviderError("test-gemini-model", 429, "quota exhausted")
    monkeypatch.setattr(d, "call_gemini", raising_caller)

    with pytest.raises(ValueError, match="No automatic paid fallback"):
        d.worker_delegate(
            "add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
            verify_cmd="true", retries=1, project_root=tmp_path)


# ---- WORKER SESSIONS ---------------------------------------------------------

def test_worker_session_resume_sends_conversation_id(tmp_path, monkeypatch):
    d._set_session_conversation("my-session", "conv-999")
    
    captured_argv = []
    
    def fake_popen(cmd, *args, **kwargs):
        captured_argv.extend(cmd)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-999", 2, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="true", retries=1, project_root=tmp_path, session_key="my-session")
                      
    assert "--conversation" in captured_argv
    assert captured_argv[captured_argv.index("--conversation") + 1] == "conv-999"


def test_worker_session_fresh_persists_id(tmp_path, monkeypatch):
    captured_argv = []
    
    def fake_popen(cmd, *args, **kwargs):
        captured_argv.extend(cmd)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "new-conv-777", 1, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="true", retries=1, project_root=tmp_path, session_key="fresh-session")
                      
    assert "--conversation" not in captured_argv
    assert d._get_session_conversation("fresh-session") == "new-conv-777"


def test_worker_session_self_healing(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_cli_bin", lambda name: "agy")
    d._set_session_conversation("stale-session", "bad-conv-id")
    
    calls = []
    
    def fake_popen(cmd, *args, **kwargs):
        calls.append(cmd)
        if "--conversation" in cmd and "bad-conv-id" in cmd:
            return FakeAgyPopen(["invalid conversation"], returncode=1)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "healed-conv-id", 1, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    out = d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                            verify_cmd="true", retries=1, project_root=tmp_path, session_key="stale-session")

    # fake_run intercepts every subprocess.run call, including the git
    # commands project_info() issues before/after the agy call — filter down
    # to just the agy invocations to prove the exact self-heal sequence
    # (1 failed resume attempt + 1 successful cold retry).
    agy_calls = [c for c in calls if c and c[0] == "agy"]
    assert len(agy_calls) == 2
    assert "--conversation" in agy_calls[0]
    assert "bad-conv-id" in agy_calls[0]
    assert "--conversation" not in agy_calls[1]
    assert d._get_session_conversation("stale-session") == "healed-conv-id"
    assert "files written : src/foo.py" in out


def test_worker_sessions_clear_cli(tmp_path):
    d._set_session_conversation("key1", "conv1")
    d._set_session_conversation("key2", "conv2")
    
    assert len(d._load_worker_sessions()) == 2
    
    # Keyed clear
    d._clear_worker_session("key1")
    sessions = d._load_worker_sessions()
    assert "key1" not in sessions
    assert "key2" in sessions
    
    # Bare clear
    d._clear_worker_session(None)
    assert len(d._load_worker_sessions()) == 0


def test_agy_self_fix_triggers_and_sends_short_delta(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_cli_bin", lambda name: "agy")
    captured_argv = []

    def fake_popen(cmd, *args, **kwargs):
        captured_argv.append(cmd)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-1", 1, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    verify_calls = [0]
    def fake_verify(cmd, cwd):
        verify_calls[0] += 1
        ok = verify_calls[0] >= 2
        rc = 0 if ok else 1
        return ok, "error output" if not ok else "", 0.1, rc
    
    monkeypatch.setattr(d, "run_verify", fake_verify)
    
    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="fake_verify", retries=1, project_root=tmp_path)
                      
    agy_calls = [c for c in captured_argv if c and c[0] == "agy"]
    assert len(agy_calls) == 2
    
    first_prompt = agy_calls[0][agy_calls[0].index("-p") + 1]
    second_prompt = agy_calls[1][agy_calls[1].index("-p") + 1]
    
    assert len(second_prompt) < len(first_prompt) / 2
    assert "verify command failed" in second_prompt
    assert "exit code: 1" in second_prompt
    assert "task" not in second_prompt
    assert "--conversation" in agy_calls[1]


def test_agy_self_fix_capped_at_one_round(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_cli_bin", lambda name: "agy")
    captured_argv = []

    def fake_popen(cmd, *args, **kwargs):
        captured_argv.append(cmd)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-1", 1, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    def fake_verify(cmd, cwd):
        return False, "error output", 0.1, 1
    
    monkeypatch.setattr(d, "run_verify", fake_verify)
    
    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="fake_verify", retries=2, project_root=tmp_path)
                      
    agy_calls = [c for c in captured_argv if c and c[0] == "agy"]
    assert len(agy_calls) == 2


def test_agy_self_fix_disabled_via_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_cli_bin", lambda name: "agy")
    captured_argv = []

    def fake_popen(cmd, *args, **kwargs):
        captured_argv.append(cmd)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-1", 1, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    def fake_verify(cmd, cwd):
        return False, "error output", 0.1, 1
    
    monkeypatch.setattr(d, "run_verify", fake_verify)
    
    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="fake_verify", retries=1, project_root=tmp_path, self_fix=False)
                      
    agy_calls = [c for c in captured_argv if c and c[0] == "agy"]
    assert len(agy_calls) == 1
    
    lines = d.AUDIT.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["self_fix_rounds"] == 0
    assert rec["self_fix_outcome"] == "skipped"


def test_agy_self_fix_ledger_outcome_fixed(tmp_path, monkeypatch):
    verify_calls = [0]
    
    def fake_popen(cmd, *args, **kwargs):
        content = "ok\n" if verify_calls[0] > 0 else "bad\n"
        lines = agy_result_lines(
            f"===FILE: src/foo.py===\n{content}===END FILE===\n===SUMMARY===\nsum\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-1", 1, 1.0
        )
        return FakeAgyPopen(lines)
        
    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    
    def fake_verify(cmd, cwd):
        verify_calls[0] += 1
        ok = verify_calls[0] >= 2
        return ok, "error", 0.1, 0 if ok else 1
    
    monkeypatch.setattr(d, "run_verify", fake_verify)
    
    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="fake_verify", retries=1, project_root=tmp_path)
                      
    lines = d.AUDIT.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["self_fix_rounds"] == 1
    assert rec["self_fix_outcome"] == "fixed"


def test_agy_self_fix_ledger_outcome_failed(tmp_path, monkeypatch):
    def fake_popen(cmd, *args, **kwargs):
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nbad\n===END FILE===\n===SUMMARY===\nsum\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-1", 1, 1.0
        )
        return FakeAgyPopen(lines)

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)

    def fake_verify(cmd, cwd):
        return False, "error", 0.1, 1

    monkeypatch.setattr(d, "run_verify", fake_verify)

    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="fake_verify", retries=1, project_root=tmp_path)

    lines = d.AUDIT.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["self_fix_rounds"] == 1
    assert rec["self_fix_outcome"] == "failed"


def test_agy_self_fix_ledger_outcome_skipped(tmp_path, monkeypatch):
    def fake_popen(cmd, *args, **kwargs):
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nok\n===END FILE===\n===SUMMARY===\nsum\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "total_tokens": 150},
            "conv-1", 1, 1.0
        )
        return FakeAgyPopen(lines)

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)

    def fake_verify(cmd, cwd):
        return True, "", 0.1, 0

    monkeypatch.setattr(d, "run_verify", fake_verify)

    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="fake_verify", retries=1, project_root=tmp_path)

    lines = d.AUDIT.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["self_fix_rounds"] == 0
    assert rec["self_fix_outcome"] == "skipped"


# justification: direct continuation of a small (<40-line), architect-written
# ledger-field fix already applied to src/delegate.py in this same review
# pass (a gap the WO's D1 explicitly required); these two tests just mirror
# the exact assertion style already used by every other test in this file.
def test_agy_ledger_carries_conversation_id_and_turns(tmp_path, monkeypatch):
    # D1: "Record num_turns and duration_seconds" — the ledger row itself
    # (not just the printed cache-hit-rate line) must be proof of warm-session
    # reuse: same conversation_id, real num_turns/duration from agy's own
    # --output-format json envelope.
    def fake_popen(cmd, *args, **kwargs):
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 42, "total_tokens": 192},
            "conv-turns-test", 3, 5.25
        )
        return FakeAgyPopen(lines)

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)

    d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="true", retries=1, project_root=tmp_path)

    rec = json.loads(d.AUDIT.read_text().strip().splitlines()[-1])
    assert rec["agy_conversation_id"] == "conv-turns-test"
    assert rec["agy_num_turns"] == 3
    assert rec["agy_duration_s"] == 5.25
    # the actual bug this WO fixes: pin/pout/cache used to be hardcoded 0/0/0
    # for every agy call — now they carry agy's real usage numbers.
    assert rec["in"] == 100
    assert rec["out"] == 50
    assert rec["cache"] == 42


def test_worker_delegate_cumulative_budget_blocks_second_self_fix_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_cli_bin", lambda name: "agy")
    calls = []

    def fake_popen(cmd, *args, **kwargs):
        calls.append(cmd)
        lines = agy_result_lines(
            "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n",
            {"input_tokens": 5000, "output_tokens": 1000, "cache_read_tokens": 0, "total_tokens": 6000},
            "conv-cumul", 1, 1.0
        )
        return FakeAgyPopen(lines)

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)

    def fake_verify(cmd, cwd):
        return False, "verify failed", 0.1, 1

    monkeypatch.setattr(d, "run_verify", fake_verify)

    # Attempt 1 uses 6000 tokens (under 8000). Verify fails, triggering self-fix.
    # For attempt 2, the remaining budget is 8000 - 6000 = 2000.
    # Call_agy_print is launched with max_tokens_per_run=2000.
    # When attempt 2 stream produces 6000 tokens (> 2000), the mid-run circuit
    # breaker kills it with BUDGET_EXCEEDED, and worker_delegate surfaces ValueError.
    with pytest.raises(ValueError, match="BUDGET_EXCEEDED"):
        d.worker_delegate("task", "agy", files_arg="src/foo.py", allow_write_arg="src/**",
                          verify_cmd="fake_verify", retries=2, project_root=tmp_path,
                          self_fix=True, max_tokens_per_run=8000)

    agy_calls = [c for c in calls if c and c[0] == "agy"]
    assert len(agy_calls) == 2


def test_worker_delegate_token_alarm_appears_for_non_agy_provider_over_budget(tmp_path, monkeypatch):
    # T-960: the post-run alarm is the ONLY protection a non-agy provider gets
    # (no streaming mid-run signal exists for call_gemini/call_openai), so it
    # must fire in the returned summary — not just the ledger — the moment a
    # run's total tokens exceed the budget, even though nothing here actually
    # aborted the call.
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"

    def huge_usage_caller(spec, key, history, system):
        return (response, spec["api"], "resp-1", 6000, 3000, 0, None)  # 9000 total

    monkeypatch.setattr(d, "call_gemini", huge_usage_caller)

    out = d.worker_delegate("add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
                            verify_cmd="true", retries=1, project_root=tmp_path,
                            max_tokens_per_run=8000)

    assert "TOKEN ALARM" in out
    assert "9,000" in out
    assert "8,000" in out


def test_worker_delegate_normal_small_run_has_no_token_alarm(tmp_path, monkeypatch):
    # The circuit breaker must be invisible to a run that never gets near the
    # budget — no alarm text, no behavior change.
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    out = d.worker_delegate("add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
                            verify_cmd="true", retries=1, project_root=tmp_path)

    assert "TOKEN ALARM" not in out
    assert "files written : src/foo.py" in out


def test_non_agy_ledger_has_no_agy_fields(tmp_path, monkeypatch):
    response = "===FILE: src/foo.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    monkeypatch.setattr(d, "call_gemini", fake_caller([response]))

    d.worker_delegate("add foo()", "test-gemini", files_arg="src/foo.py", allow_write_arg="src/**",
                      verify_cmd="true", retries=1, project_root=tmp_path)

    rec = json.loads(d.AUDIT.read_text().strip().splitlines()[-1])
    assert "agy_conversation_id" not in rec
    assert "agy_num_turns" not in rec
    assert "agy_duration_s" not in rec
