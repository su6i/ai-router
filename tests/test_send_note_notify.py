import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import delegate as d


@pytest.fixture
def mock_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "fake_chat_id")
    projects_dir = tmp_path / "agent-projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(d, "AGENT_PROJECTS", projects_dir)
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    return projects_dir


@pytest.fixture
def mock_dashboards(monkeypatch):
    dashboards = MagicMock()
    monkeypatch.setitem(sys.modules, "dashboards", dashboards)
    return dashboards.send_note_ping_deduped, dashboards.push_dashboard


def test_send_note_arix_no_notify(mock_env, mock_dashboards):
    ping_mock, _ = mock_dashboards
    target = mock_env / "arix"
    target.mkdir()
    
    d.send_note("arix", "hello")
    
    assert ping_mock.call_count == 0
    inbox = target / "workspace" / "inbox"
    assert len(list(inbox.glob("NOTE-*.md"))) == 1


def test_send_note_owner_no_notify(mock_env, mock_dashboards):
    ping_mock, _ = mock_dashboards
    target = mock_env / "@-github"
    target.mkdir()
    
    d.send_note("@-github", "hello")
    
    assert ping_mock.call_count == 1
    inbox = target / "workspace" / "inbox"
    assert len(list(inbox.glob("NOTE-*.md"))) == 1


def test_send_note_arix_explicit_notify(mock_env, mock_dashboards):
    ping_mock, _ = mock_dashboards
    target = mock_env / "arix"
    target.mkdir()
    
    d.send_note("arix", "hello", notify=True)
    
    assert ping_mock.call_count == 1
    inbox = target / "workspace" / "inbox"
    assert len(list(inbox.glob("NOTE-*.md"))) == 1


def test_send_note_broadcast(mock_env, mock_dashboards):
    ping_mock, _ = mock_dashboards
    projects = [f"proj_{i}" for i in range(7)]
    for p in projects:
        (mock_env / p).mkdir()
        
    for p in projects:
        d.send_note(p, "broadcast")
        
    assert ping_mock.call_count == 0
    for p in projects:
        inbox = mock_env / p / "workspace" / "inbox"
        assert len(list(inbox.glob("NOTE-*.md"))) == 1
