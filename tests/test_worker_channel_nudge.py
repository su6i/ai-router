import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks import worker_channel_nudge

import io
import uuid

def run_hook(monkeypatch, tool_name, command, session_id=None):
    if session_id is None:
        session_id = f"test-session-{uuid.uuid4()}"
    payload = {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {"command": command} if command else {}
    }
    
    output = []
    def fake_print(s):
        output.append(s)
        
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("builtins.print", fake_print)
    
    worker_channel_nudge.main()
    return json.loads(output[0]) if output else None

def test_nudge_denies_agy_print(monkeypatch):
    out = run_hook(monkeypatch, "Bash", "agy -p 'do it'")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "router-only workers" in out["hookSpecificOutput"]["permissionDecisionReason"]

def test_nudge_allows_second_attempt(monkeypatch):
    run_hook(monkeypatch, "Bash", "agy --print 'do it'", "session-1")
    out2 = run_hook(monkeypatch, "Bash", "agy --print 'do it'", "session-1")
    assert out2 is None  # allowed

def test_nudge_allows_interactive_agy(monkeypatch):
    out = run_hook(monkeypatch, "Bash", "agy 'hello'")
    assert out is None

def test_nudge_denies_codewhale_exec(monkeypatch):
    out = run_hook(monkeypatch, "Bash", "codewhale exec --auto 'hello'")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

def test_nudge_allows_non_worker_bash(monkeypatch):
    out = run_hook(monkeypatch, "Bash", "ls -la")
    assert out is None

def test_nudge_fail_open_malformed_input(monkeypatch):
    monkeypatch.setattr(worker_channel_nudge.json, "load", lambda x: 1/0)
    # Should not crash
    worker_channel_nudge.main()
