import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d

def test_route_task_refuses_push(monkeypatch):
    called = False
    
    def fake_agent_delegate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("agent_delegate should not have been called")
        
    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)
    
    task_note = {
        "repo": "/tmp/repo",
        "goal": "do some work and please push to main when done",
        "constraints": "",
        "priority": "normal",
        "from_project": None
    }
    
    with pytest.raises(ValueError, match="route_task refuses: task-note requests push/merge"):
        d.route_task(task_note, verify_cmd="")
        
    assert not called

def test_route_task_refuses_merge(monkeypatch):
    called = False
    
    def fake_agent_delegate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("agent_delegate should not have been called")
        
    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)
    
    task_note = {
        "repo": "/tmp/repo",
        "goal": "do some work",
        "constraints": "git merge origin/main",
        "priority": "normal",
        "from_project": None
    }
    
    with pytest.raises(ValueError, match="route_task refuses: task-note requests push/merge"):
        d.route_task(task_note, verify_cmd="")
        
    assert not called


def test_route_task_success_never_calls_paid_fallback(monkeypatch):
    """A clean agy success must not touch codewhale at all — the $0-first
    ladder only escalates on verify-fail or exception, never by default."""
    calls = []

    def fake_agent_delegate(task, runner, **kwargs):
        calls.append(runner)
        assert runner == "agy"
        return "runner        : agy (Gemini 3.1 Pro (High))\nstatus        : COMPLETED (1.0s)"

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)

    task_note = {"repo": "/tmp/repo", "goal": "do some harmless work", "from_project": None}
    report = d.route_task(task_note, verify_cmd="true")

    assert calls == ["agy"]
    assert "COMPLETED" in report


def test_route_task_falls_back_to_codewhale_on_verify_fail(monkeypatch):
    calls = []

    def fake_agent_delegate(task, runner, **kwargs):
        calls.append(runner)
        if runner == "agy":
            return "status        : COMPLETED — ⚠️ VERIFY FAILED"
        assert runner == "codewhale"
        assert kwargs.get("model") == "flash"
        return "status        : COMPLETED (codewhale flash)"

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)

    task_note = {"repo": "/tmp/repo", "goal": "do some work", "from_project": None}
    report = d.route_task(task_note, verify_cmd="pytest -q")

    assert calls == ["agy", "codewhale"]
    assert "Paid fallback used (codewhale flash)" in report
    assert "verify failed" in report


def test_route_task_falls_back_to_codewhale_on_exception(monkeypatch):
    calls = []

    def fake_agent_delegate(task, runner, **kwargs):
        calls.append(runner)
        if runner == "agy":
            raise ValueError("channel agy disabled in channels.json")
        return "status        : COMPLETED (codewhale flash)"

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)

    task_note = {"repo": "/tmp/repo", "goal": "do some work", "from_project": None}
    report = d.route_task(task_note)

    assert calls == ["agy", "codewhale"]
    assert "Paid fallback used (codewhale flash)" in report
    assert "channel agy disabled" in report


def test_route_task_falls_back_on_quota_systemexit(monkeypatch):
    """agent_delegate's own check_budget() aborts a daily-cap/quota hit via
    sys.exit (SystemExit), not a raised Exception — the WO explicitly lists
    'a free-tier quota hit' as a fallback trigger alongside verify-fail, so a
    bare `except Exception` would silently miss this and crash the process."""
    calls = []

    def fake_agent_delegate(task, runner, **kwargs):
        calls.append(runner)
        if runner == "agy":
            raise SystemExit("❌ BUDGET ABORT: daily call cap exceeded for google-ai-pro (10 > 10)")
        return "status        : COMPLETED (codewhale flash)"

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)

    task_note = {"repo": "/tmp/repo", "goal": "do some work", "from_project": None}
    report = d.route_task(task_note)

    assert calls == ["agy", "codewhale"]
    assert "Paid fallback used (codewhale flash)" in report
    assert "BUDGET ABORT" in report


def test_route_task_both_runners_fail_reports_both(monkeypatch):
    def fake_agent_delegate(task, runner, **kwargs):
        if runner == "agy":
            raise ValueError("agy down")
        raise ValueError("codewhale down too")

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)

    task_note = {"repo": "/tmp/repo", "goal": "do some work", "from_project": None}
    report = d.route_task(task_note)

    assert "agy down" in report
    assert "codewhale down too" in report
    assert "also failed" in report


def test_route_task_reports_back_to_manager_inbox(monkeypatch):
    sent = {}

    def fake_agent_delegate(task, runner, **kwargs):
        return "status        : COMPLETED (1.0s)"

    def fake_send_note(to_project, message, priority="normal", subject=""):
        sent["to_project"] = to_project
        sent["message"] = message
        sent["priority"] = priority
        sent["subject"] = subject
        return "note sent"

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)
    monkeypatch.setattr(d, "send_note", fake_send_note)

    task_note = {
        "repo": "/tmp/repo",
        "goal": "do some work",
        "priority": "high",
        "from_project": "manager"
    }
    report = d.route_task(task_note)

    assert sent["to_project"] == "manager"
    assert sent["priority"] == "high"
    assert sent["subject"] == "task-note result"
    assert "COMPLETED" in sent["message"]
    assert report == sent["message"]


def test_route_task_no_report_back_without_from_project(monkeypatch):
    def fake_agent_delegate(task, runner, **kwargs):
        return "status        : COMPLETED (1.0s)"

    def fake_send_note(*args, **kwargs):
        raise AssertionError("send_note must not be called when from_project is unset")

    monkeypatch.setattr(d, "agent_delegate", fake_agent_delegate)
    monkeypatch.setattr(d, "send_note", fake_send_note)

    task_note = {"repo": "/tmp/repo", "goal": "do some work"}
    d.route_task(task_note)  # must not raise


def test_parse_task_note_file(tmp_path):
    note = tmp_path / "NOTE-2026-07-21-manager-taskA.md"
    note.write_text(
        "---\n"
        "from: manager\n"
        "to: ai-router\n"
        "priority: high\n"
        "read: false\n"
        "---\n"
        "\n"
        "repo: /Users/su6i/@-github/ai-router\n"
        "Fix the flaky test and open a report.\n"
    )

    task_note = d.parse_task_note_file(note)

    assert task_note["repo"] == "/Users/su6i/@-github/ai-router"
    assert task_note["goal"] == "Fix the flaky test and open a report."
    assert task_note["priority"] == "high"
    assert task_note["from_project"] == "manager"


def test_parse_task_note_file_missing_repo_raises(tmp_path):
    note = tmp_path / "NOTE-no-repo.md"
    note.write_text("---\nfrom: manager\n---\n\nJust a goal with no repo line.\n")

    with pytest.raises(ValueError, match="missing 'repo:"):
        d.parse_task_note_file(note)
