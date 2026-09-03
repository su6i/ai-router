import io
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks import code_lookup_gate

def run_hook(monkeypatch, payload):
    output = []
    def fake_print(s):
        output.append(s)
        
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("builtins.print", fake_print)
    
    code_lookup_gate.main()
    return json.loads(output[0]) if output else None

def test_pass_small_file(monkeypatch, tmp_path):
    small_file = tmp_path / "small.py"
    small_file.write_text("print('hello')\n")
    
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": "",
        "tool_name": "Read",
        "tool_input": {"file_path": str(small_file)}
    }
    
    result = run_hook(monkeypatch, payload)
    assert result is None

def test_pass_recent_code_lookup(monkeypatch, tmp_path):
    large_file = tmp_path / "large.py"
    large_file.write_text("x\n" * 9000)
    
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "mcp__ai-router__code_lookup", "input": {}}
            ]
        }
    }) + "\n")
    
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }
    
    result = run_hook(monkeypatch, payload)
    assert result is None

def test_block_large_file_no_recent_lookup(monkeypatch, tmp_path):
    large_file = tmp_path / "large2.py"
    large_file.write_text("x\n" * 9000)
    
    transcript = tmp_path / "transcript2.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {}}
            ]
        }
    }) + "\n")
    
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }
    
    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert str(large_file) in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mcp__ai-router__code_lookup" in result["hookSpecificOutput"]["permissionDecisionReason"]

def test_second_attempt_override(monkeypatch, tmp_path):
    large_file = tmp_path / "large3.py"
    large_file.write_text("x\n" * 9000)
    
    transcript = tmp_path / "transcript3.jsonl"
    transcript.write_text("")
    
    session_id = str(uuid.uuid4())
    payload = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }
    
    # First attempt blocks
    res1 = run_hook(monkeypatch, payload)
    assert res1 is not None
    assert res1["hookSpecificOutput"]["permissionDecision"] == "deny"
    
    # Second attempt passes
    res2 = run_hook(monkeypatch, payload)
    assert res2 is None

def test_fail_open_malformed_transcript(monkeypatch, tmp_path):
    large_file = tmp_path / "large4.py"
    large_file.write_text("x\n" * 9000)
    
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(tmp_path / "does_not_exist.jsonl"),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }
    
    result = run_hook(monkeypatch, payload)
    assert result is None  # Should fail open

def test_fail_open_garbage_transcript_content(monkeypatch, tmp_path):
    large_file = tmp_path / "large6.py"
    large_file.write_text("x\n" * 9000)

    transcript = tmp_path / "transcript6.jsonl"
    transcript.write_text("not json at all\n{{{broken\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }

    result = run_hook(monkeypatch, payload)
    assert result is None  # every line unparseable -> cannot verify -> fail open

def test_block_message_uses_target_files_repo(monkeypatch, tmp_path):
    repo_dir = tmp_path / "myrepo"
    (repo_dir / ".git").mkdir(parents=True)
    sub_dir = repo_dir / "sub"
    sub_dir.mkdir()
    large_file = sub_dir / "big.py"
    large_file.write_text("x\n" * 9000)

    transcript = tmp_path / "transcript_repo.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert 'repo="myrepo"' in reason

def test_block_message_omits_repo_with_no_git_root(monkeypatch, tmp_path):
    # tmp_path has no .git anywhere in its ancestry.
    large_file = tmp_path / "orphan.py"
    large_file.write_text("x\n" * 9000)

    transcript = tmp_path / "transcript_norepo.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert 'repo="' not in reason

def test_transcript_tail_cap_ignores_old_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "TRANSCRIPT_TAIL_BYTES", 300)

    large_file = tmp_path / "large_tail1.py"
    large_file.write_text("x\n" * 9000)

    old_lookup_line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "mcp__ai-router__code_lookup", "input": {}}
        ]}
    })
    padding_line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 60}}
        ]}
    })

    transcript = tmp_path / "big_transcript.jsonl"
    lines = [old_lookup_line] + [padding_line] * 30
    transcript.write_text("\n".join(lines) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }

    result = run_hook(monkeypatch, payload)
    # the code_lookup call is far outside the tail window -> not seen -> block
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

def test_transcript_tail_cap_still_sees_recent_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "TRANSCRIPT_TAIL_BYTES", 300)

    large_file = tmp_path / "large_tail2.py"
    large_file.write_text("x\n" * 9000)

    padding_line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 60}}
        ]}
    })
    recent_lookup_line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "mcp__ai-router__code_lookup", "input": {}}
        ]}
    })

    transcript = tmp_path / "big_transcript2.jsonl"
    lines = [padding_line] * 30 + [recent_lookup_line]
    transcript.write_text("\n".join(lines) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }

    result = run_hook(monkeypatch, payload)
    # the code_lookup call is within the tail window -> seen -> allow
    assert result is None

def test_pass_recently_written_file(monkeypatch, tmp_path):
    large_file = tmp_path / "large5.py"
    large_file.write_text("x\n" * 9000)
    
    transcript = tmp_path / "transcript5.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": str(large_file)}}
            ]
        }
    }) + "\n")
    
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Read",
        "tool_input": {"file_path": str(large_file)}
    }
    
    result = run_hook(monkeypatch, payload)
    assert result is None
