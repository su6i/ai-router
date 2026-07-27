import sys
from pathlib import Path
from unittest.mock import MagicMock
import json

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d
import dashboards

@pytest.fixture
def fake_agent_projects(tmp_path, monkeypatch):
    root = tmp_path / "agent-projects"
    root.mkdir()
    monkeypatch.setattr(d, "AGENT_PROJECTS", root)
    
    test_audit = root / "test_audit.log"
    monkeypatch.setattr(d, "AUDIT", test_audit)

    data_dir = root / "data"
    data_dir.mkdir()
    monkeypatch.setattr(d, "DATA_DIR", data_dir)
    
    return root

def test_peek_never_marks_read(fake_agent_projects):
    proj = fake_agent_projects / "testproj"
    proj.mkdir()
    d.send_note("testproj", "hello world", notify=False)
    
    out = dashboards.render_inbox()
    assert "testproj" in out
    
    inbox = proj / "workspace" / "inbox"
    notes = list(inbox.glob("NOTE-*.md"))
    assert len(notes) == 1
    content = notes[0].read_text()
    assert "read: false" in content
    assert "read: true" not in content

def test_render_tasks_parses_queue(fake_agent_projects):
    queue_file = fake_agent_projects / "_memory" / "QUEUE.md"
    queue_file.parent.mkdir(parents=True)
    
    content = """## Some heading

## 🎯 ترتیبِ اجرا
| No | Tier | Repo | Context | Status/Blocker |
|----|------|------|---------|----------------|
| 1 | P1 | repo1 | | block1 |
| 2 | P2✅ | repo2 | | block2 |
| malformed
| 3 | P3 | repo3 | | block3 |

## Other section
| No | Tier | Repo |
| 4 | P4 | repo4 |
"""
    queue_file.write_text(content, encoding="utf-8")
    out = dashboards.render_tasks()
    
    assert "repo1" in out
    assert "repo2" not in out
    assert "⚠️ QUEUE.md row 5 unparsed" in out
    assert "repo3" in out
    assert "repo4" not in out
    assert "Other section" not in out

def test_html_escaping(fake_agent_projects):
    proj = fake_agent_projects / "testproj"
    proj.mkdir()
    d.send_note("testproj", "<b>&", subject="<b>&", notify=False)
    out = dashboards.render_inbox()
    assert "&lt;b&gt;&amp;" in out
    assert "<b>&" not in out

def test_dashboards_truncation(fake_agent_projects):
    proj = fake_agent_projects / "testproj"
    proj.mkdir()
    for i in range(300):
        d.send_note("testproj", f"msg {i}", subject=f"subject {i} with a very very very long title to force truncation", notify=False)
    
    out = dashboards.render_inbox()
    assert len(out) <= 4096
    assert "more (see" in out

def test_first_push(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock()
    def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "sendMessage" in url:
            resp.json.return_value = {"ok": True, "result": {"message_id": 111}}
        elif "pinChatMessage" in url:
            resp.json.return_value = {"ok": True, "result": {}}
        return resp
    mock_post.side_effect = side_effect
    monkeypatch.setattr("httpx.post", mock_post)
    
    res = dashboards.push_dashboard("inbox")
    assert "message_id=111" in res
    assert mock_post.call_count == 2
    
    urls = [call[0][0] for call in mock_post.call_args_list]
    assert "sendMessage" in urls[0]
    assert "pinChatMessage" in urls[1]
    
    state_file = d.DATA_DIR / "telegram_dashboards.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["inbox"]["message_id"] == 111
    assert "hash" in state["inbox"]

def test_second_push_identical(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock()
    def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "sendMessage" in url:
            resp.json.return_value = {"ok": True, "result": {"message_id": 111}}
        elif "pinChatMessage" in url:
            resp.json.return_value = {"ok": True, "result": {}}
        return resp
    mock_post.side_effect = side_effect
    monkeypatch.setattr("httpx.post", mock_post)
    
    dashboards.push_dashboard("inbox")
    assert mock_post.call_count == 2
    
    mock_post.reset_mock()
    res = dashboards.push_dashboard("inbox")
    assert "unchanged" in res
    assert mock_post.call_count == 0

def test_changed_content_editMessageText(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock()
    def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "sendMessage" in url:
            resp.json.return_value = {"ok": True, "result": {"message_id": 111}}
        elif "pinChatMessage" in url:
            resp.json.return_value = {"ok": True, "result": {}}
        elif "editMessageText" in url:
            resp.json.return_value = {"ok": True, "result": {}}
        return resp
    mock_post.side_effect = side_effect
    monkeypatch.setattr("httpx.post", mock_post)
    
    proj = fake_agent_projects / "testproj"
    proj.mkdir()
    d.send_note("testproj", "a", notify=False)
    
    dashboards.push_dashboard("inbox")
    
    mock_post.reset_mock()
    d.send_note("testproj", "b", notify=False)
    res = dashboards.push_dashboard("inbox")
    assert "edited" in res
    
    assert mock_post.call_count == 1
    assert "editMessageText" in mock_post.call_args[0][0]
    assert mock_post.call_args[1]["json"]["message_id"] == 111

def test_message_not_found_fallback(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    state = {"inbox": {"message_id": 999, "hash": "oldhash"}}
    (d.DATA_DIR / "telegram_dashboards.json").write_text(json.dumps(state))
    
    mock_post = MagicMock()
    def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "editMessageText" in url:
            resp.json.return_value = {"ok": False, "description": "Bad Request: message to edit not found"}
        elif "sendMessage" in url:
            resp.json.return_value = {"ok": True, "result": {"message_id": 112}}
        elif "pinChatMessage" in url:
            resp.json.return_value = {"ok": True, "result": {}}
        return resp
    mock_post.side_effect = side_effect
    monkeypatch.setattr("httpx.post", mock_post)
    
    res = dashboards.push_dashboard("inbox")
    assert "recreated" in res
    assert "112" in res
    
    final_state = json.loads((d.DATA_DIR / "telegram_dashboards.json").read_text())
    assert final_state["inbox"]["message_id"] == 112

def test_ok_false_raises(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": False, "description": "chat not found"}
    mock_post.return_value = resp
    monkeypatch.setattr("httpx.post", mock_post)
    
    with pytest.raises(RuntimeError, match="chat not found"):
        dashboards.push_dashboard("inbox")

def test_bot_token_redacted(fake_agent_projects, monkeypatch):
    secret = "SUPER-SECRET-TOKEN-XYZ"
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", secret)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": False, "description": f"chat not found {secret}"}
    mock_post.return_value = resp
    monkeypatch.setattr("httpx.post", mock_post)
    
    with pytest.raises(RuntimeError) as excinfo:
        dashboards.push_dashboard("inbox")
    assert secret not in str(excinfo.value)
    
    mock_post.side_effect = httpx.RequestError(f"Network error with token {secret}")
    with pytest.raises(RuntimeError) as excinfo2:
        dashboards.push_dashboard("inbox")
    assert secret not in str(excinfo2.value)

def test_send_note_notify_false(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr("httpx.post", mock_post)
    
    proj = fake_agent_projects / "testproj"
    proj.mkdir()
    d.send_note("testproj", "hello", notify=False)
    assert mock_post.call_count == 0

def test_send_note_notify_true_fails_gracefully(fake_agent_projects, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    
    mock_post = MagicMock(side_effect=httpx.RequestError("Network error"))
    monkeypatch.setattr("httpx.post", mock_post)
    
    proj = fake_agent_projects / "testproj"
    proj.mkdir()
    res = d.send_note("testproj", "hello", notify=True)
    assert "note sent" in res
    
    notes = list((proj / "workspace" / "inbox").glob("NOTE-*.md"))
    assert len(notes) == 1
