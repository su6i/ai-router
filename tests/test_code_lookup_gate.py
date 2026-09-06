import io
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks import code_lookup_gate

@pytest.fixture(autouse=True)
def _isolated_decision_log(monkeypatch, tmp_path):
    # Every gated call in this file's tests exercises _log_decision. Without
    # this, they'd all append to the real AI_ROUTER_LOOKUP_GATE_LOG default
    # (the host's system tempdir) on every test run forever -- a real-world
    # side effect this suite should never leave behind. Point it at a
    # throwaway file inside pytest's own tmp_path instead.
    monkeypatch.setenv("AI_ROUTER_LOOKUP_GATE_LOG", str(tmp_path / "gate-test.log"))

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
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
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

def test_kill_switch_disables_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_ROUTER_LOOKUP_GATE", "off")

    large_file = tmp_path / "ks_large.py"
    large_file.write_text("x\n" * 9000)

    transcript = tmp_path / "ks_transcript.jsonl"
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
    assert result is None

def test_empty_index_passes_through(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: False)

    large_file = tmp_path / "ei_large.py"
    large_file.write_text("x\n" * 9000)

    transcript = tmp_path / "ei_transcript.jsonl"
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
    assert result is None

def test_populated_index_still_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)

    large_file = tmp_path / "pi_large.py"
    large_file.write_text("x\n" * 9000)

    transcript = tmp_path / "pi_transcript.jsonl"
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

def test_repo_has_chunks_fails_open_on_missing_dsn(monkeypatch):
    # _repo_has_chunks calls delegate.load_env() before checking POSTGRES_DSN,
    # and load_env() repopulates POSTGRES_DSN from the owner's real vault
    # secrets when the var is absent from os.environ. Deleting the env var
    # alone would therefore reintroduce a real DSN and attempt a real DB
    # connection here -- neutralise load_env() itself so this test never
    # touches the vault or the network, regardless of what's actually
    # installed on the host running it.
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr("delegate.load_env", lambda: None, raising=False)
    result = code_lookup_gate._repo_has_chunks("some-repo")
    assert result is False

def test_log_decision_writes_json_line(monkeypatch, tmp_path):
    log_file = tmp_path / "gate.log"
    monkeypatch.setenv("AI_ROUTER_LOOKUP_GATE_LOG", str(log_file))
    code_lookup_gate._log_decision("r", "Read", "/x", "block")
    lines = log_file.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry == {"repo": "r", "tool": "Read", "target": "/x", "decision": "block"}

def test_grep_blocks_without_recent_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)

    transcript = tmp_path / "grep_block_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Grep",
        "tool_input": {"pattern": "foo", "path": str(tmp_path)},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

def test_grep_second_attempt_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)

    transcript = tmp_path / "grep_second_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    session_id = str(uuid.uuid4())
    payload = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "tool_name": "Grep",
        "tool_input": {"pattern": "bar_unique", "path": str(tmp_path)},
        "cwd": str(tmp_path),
    }

    res1 = run_hook(monkeypatch, payload)
    assert res1 is not None
    assert res1["hookSpecificOutput"]["permissionDecision"] == "deny"

    res2 = run_hook(monkeypatch, payload)
    assert res2 is None

def test_glob_blocks_without_recent_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)

    transcript = tmp_path / "glob_block_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Glob",
        "tool_input": {"pattern": "**/*.py", "path": str(tmp_path)},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

def test_grep_passes_with_recent_code_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)

    transcript = tmp_path / "grep_pass_lookup_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "mcp__ai-router__code_lookup", "input": {}}
        ]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Grep",
        "tool_input": {"pattern": "foo", "path": str(tmp_path)},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None

def test_grep_empty_index_passes_through(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: False)

    transcript = tmp_path / "grep_empty_idx_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Grep",
        "tool_input": {"pattern": "unique_empty_idx_pattern", "path": str(tmp_path)},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None

def test_grep_uses_cwd_when_no_path_given(monkeypatch, tmp_path):
    called = []

    def fake_repo_has_chunks(repo):
        called.append(repo)
        return True

    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", fake_repo_has_chunks)

    transcript = tmp_path / "grep_cwd_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Grep",
        "tool_input": {"pattern": "foo"},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert called, "_repo_has_chunks was never called"
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

# ---------------------------------------------------------------------------
# Bash gating tests
# ---------------------------------------------------------------------------

def _bash_repo_setup(tmp_path):
    """Create a minimal fake repo under tmp_path and return (repo_dir, transcript_path).
    The transcript has a single unrelated Bash tool_use so recency never passes."""
    repo_dir = tmp_path / "bashrepo"
    (repo_dir / ".git").mkdir(parents=True)
    transcript = tmp_path / "bash_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")
    return repo_dir, transcript


def test_bash_cat_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    repo_dir, transcript = _bash_repo_setup(tmp_path)
    large_file = repo_dir / "large.py"
    large_file.write_text("x\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "cat " + str(large_file)},
        "cwd": str(repo_dir),
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_grep_direct_file_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    repo_dir, transcript = _bash_repo_setup(tmp_path)
    some_file = repo_dir / "src.py"
    some_file.write_text("# TODO: fix\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "grep TODO " + str(some_file)},
        "cwd": str(repo_dir),
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_sed_n_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    repo_dir, transcript = _bash_repo_setup(tmp_path)
    some_file = repo_dir / "data.py"
    some_file.write_text("line\n" * 100)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "sed -n '1,50p' " + str(some_file)},
        "cwd": str(repo_dir),
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_find_name_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    repo_dir, transcript = _bash_repo_setup(tmp_path)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "find " + str(repo_dir) + " -name '*.py'"},
        "cwd": str(repo_dir),
    }

    result = run_hook(monkeypatch, payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_second_attempt_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    repo_dir, transcript = _bash_repo_setup(tmp_path)
    large_file = repo_dir / "large2.py"
    large_file.write_text("x\n")

    session_id = str(uuid.uuid4())
    payload = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "cat " + str(large_file)},
        "cwd": str(repo_dir),
    }

    res1 = run_hook(monkeypatch, payload)
    assert res1 is not None
    assert res1["hookSpecificOutput"]["permissionDecision"] == "deny"

    res2 = run_hook(monkeypatch, payload)
    assert res2 is None


def test_bash_git_log_grep_not_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    _repo_dir, transcript = _bash_repo_setup(tmp_path)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "git log --grep=fixme"},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None


def test_bash_heredoc_not_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    _repo_dir, transcript = _bash_repo_setup(tmp_path)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "cat <<EOF\nsome text\nEOF"},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None


def test_bash_pipe_non_filesystem_source_not_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    _repo_dir, transcript = _bash_repo_setup(tmp_path)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "history | grep foo"},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None


def test_bash_grep_over_command_output_not_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    _repo_dir, transcript = _bash_repo_setup(tmp_path)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "git diff | grep TODO"},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None


def test_bash_no_git_root_not_gated(monkeypatch, tmp_path):
    # tmp_path has no .git anywhere in its ancestry — _find_repo_name returns None.
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    orphan_file = tmp_path / "orphan.py"
    orphan_file.write_text("x\n")

    transcript = tmp_path / "bash_nogit_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}
    }) + "\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "cat " + str(orphan_file)},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None


def test_bash_empty_index_passes_through(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: False)
    repo_dir, transcript = _bash_repo_setup(tmp_path)
    some_file = repo_dir / "file.py"
    some_file.write_text("x\n")

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "cat " + str(some_file)},
        "cwd": str(repo_dir),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None


def test_bash_bare_cat_no_target_not_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(code_lookup_gate, "_repo_has_chunks", lambda repo: True)
    _repo_dir, transcript = _bash_repo_setup(tmp_path)

    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "cat"},
        "cwd": str(tmp_path),
    }

    result = run_hook(monkeypatch, payload)
    assert result is None
