import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import json
import delegate as d

@pytest.fixture
def fake_agent_projects(tmp_path, monkeypatch):
    root = tmp_path / "agent-projects"
    root.mkdir()
    monkeypatch.setattr(d, "AGENT_PROJECTS", root)
    
    # Create fake project 'arix'
    arix = root / "arix"
    arix.mkdir()
    
    # Use a separate test audit log to not pollute anything
    test_audit = root / "test_audit.log"
    monkeypatch.setattr(d, "AUDIT", test_audit)
    
    return root

def test_send_note_unknown_project(fake_agent_projects):
    with pytest.raises(ValueError, match="unknown project: missing"):
        d.send_note("missing", "hello")

def test_send_note_path_traversal(fake_agent_projects):
    with pytest.raises(ValueError, match="invalid project name: \\.\\./escaping"):
        d.send_note("../escaping", "hello")
        
    with pytest.raises(ValueError, match="invalid project name: escaping/path"):
        d.send_note("escaping/path", "hello")

def test_send_note_writes_file_and_redacts(fake_agent_projects, monkeypatch):
    # setup fake secret
    monkeypatch.setenv("FAKE_SECRET_KEY", "super_secret_value_12345")
    # patch MODELS to include a fake model config with that key
    monkeypatch.setitem(d.MODELS, "fake", {"key": "FAKE_SECRET_KEY"})
    
    arix_root = fake_agent_projects / "arix"
    
    msg = "here is the key: super_secret_value_12345"
    d.send_note("arix", msg, subject="Test Note")
    
    inbox = arix_root / "workspace" / "inbox"
    assert inbox.exists()
    
    files = list(inbox.glob("NOTE-*.md"))
    assert len(files) == 1
    
    content = files[0].read_text()
    # Check front matter
    assert "to: arix" in content
    assert "priority: normal" in content
    assert "read: false" in content
    assert "subject: Test Note" in content
    
    # Check redaction
    assert "super_secret_value_12345" not in content
    assert "<FAKE_SECRET_KEY...2345>" in content

def test_list_notes_and_peek(fake_agent_projects):
    
    # send two notes
    d.send_note("arix", "msg 1", subject="Subject 1")
    d.send_note("arix", "msg 2", subject="Subject 2")
    
    # --peek doesn't mark read
    notes_peek = d.list_notes("arix", unread_only=True, peek=True)
    assert len(notes_peek) == 2
    
    # still 2 notes because it was a peek
    notes_peek2 = d.list_notes("arix", unread_only=True, peek=True)
    assert len(notes_peek2) == 2
    
    # now really list them
    notes = d.list_notes("arix", unread_only=True, peek=False)
    assert len(notes) == 2
    
    # next list unread_only=True should return 0
    notes_empty = d.list_notes("arix", unread_only=True, peek=False)
    assert len(notes_empty) == 0
    
    # but unread_only=False returns 2
    notes_all = d.list_notes("arix", unread_only=False, peek=False)
    assert len(notes_all) == 2
    
    # Check idempotence: files should still have read: true
    for f, meta, body in notes_all:
        assert f.read_text().count("read: true") == 1
        assert "read: false" not in f.read_text()

def test_audit_logs(fake_agent_projects):
    d.send_note("arix", "hello audit")
    
    d.list_notes("arix", unread_only=True, peek=False)
    
    lines = d.AUDIT.read_text().strip().split("\n")
    assert len(lines) == 2
    
    send_rec = json.loads(lines[0])
    assert send_rec["action"] == "send"
    assert send_rec["mode"] == "note"
    assert send_rec["to"] == "arix"
    
    read_rec = json.loads(lines[1])
    assert read_rec["action"] == "read"
    assert read_rec["mode"] == "note"
    assert read_rec["to"] == "arix"
