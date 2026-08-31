import json
from pathlib import Path

import delegate

import pytest

import doctor

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_DOCTOR_HOME", str(tmp_path))
    return tmp_path

def test_isolation(fake_home):
    # The suite must never read or write the real ~/.claude.json — prove it
    # with a real, executable assertion (not just that the path getter
    # returns the fake path): the real file's mtime must be byte-identical
    # before and after every check that touches "home" runs against the
    # fake one.
    real_claude_json = Path.home() / ".claude.json"
    real_mtime_before = real_claude_json.stat().st_mtime if real_claude_json.exists() else None

    (fake_home / ".claude.json").write_text("{broken")
    doctor.check_mcp_registration()
    with pytest.raises(SystemExit):
        doctor.fix_mcp_registration()

    assert doctor._get_claude_json_path() == fake_home / ".claude.json"
    if real_mtime_before is not None:
        assert real_claude_json.stat().st_mtime == real_mtime_before
    else:
        assert not real_claude_json.exists()

def test_mcp_registration(fake_home, capsys):
    p = fake_home / ".claude.json"
    
    assert not doctor.check_mcp_registration()
    assert "FAIL" in capsys.readouterr().out
    
    p.write_text("{broken")
    assert not doctor.check_mcp_registration()
    assert "Unparseable" in capsys.readouterr().out
    
    p.write_text('{"mcpServers": []}')
    assert not doctor.check_mcp_registration()
    assert "not a JSON object" in capsys.readouterr().out
    
    p.write_text('{"mcpServers": {}}')
    assert not doctor.check_mcp_registration()
    assert "MISSING — not registered" in capsys.readouterr().out
    
    p.write_text(json.dumps({"mcpServers": {"ai-router": {"type": "stdio", "args": ["/wrong"]}}}))
    assert not doctor.check_mcp_registration()
    assert "STALE-PATH" in capsys.readouterr().out
    
    p.write_text(json.dumps({"mcpServers": {"ai-router": {"type": "stdio", "args": [str(doctor.REPO_ROOT / "mcp" / "server.py")]}}}))
    assert doctor.check_mcp_registration()
    assert "OK" in capsys.readouterr().out

def test_mcp_fix(fake_home):
    p = fake_home / ".claude.json"
    
    p.write_text("{broken")
    with pytest.raises(SystemExit):
        doctor.fix_mcp_registration()
    assert p.read_text() == "{broken"
    
    initial_data = {
        "userID": "abc123",
        "mcpServers": {
            "other": {"type": "stdio", "args": ["foo"]}
        }
    }
    p.write_text(json.dumps(initial_data))
    doctor.fix_mcp_registration()
    
    fixed_data = json.loads(p.read_text())
    assert fixed_data["userID"] == "abc123"
    assert fixed_data["mcpServers"]["other"] == {"type": "stdio", "args": ["foo"]}
    assert fixed_data["mcpServers"]["ai-router"]["args"][0] == str(doctor.REPO_ROOT / "mcp" / "server.py")
    
    doctor.fix_mcp_registration()
    assert json.loads(p.read_text()) == fixed_data

def test_mcp_handshake(capsys):
    assert doctor.check_mcp_handshake()
    assert "OK" in capsys.readouterr().out

def test_hooks_exist(fake_home, capsys):
    p = fake_home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    
    valid_py = fake_home / "valid.py"
    valid_py.write_text("print('ok')")
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"type": "command", "command": f"python3 {valid_py}"}]}}))
    assert doctor.check_hooks_exist()
    assert "OK" in capsys.readouterr().out
    
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"type": "command", "command": f"python3 {fake_home}/nonexistent.py"}]}}))
    assert not doctor.check_hooks_exist()
    assert "FAIL" in capsys.readouterr().out
    
    err_py = fake_home / "err.py"
    err_py.write_text("def f(:\n")
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"type": "command", "command": f"python3 {err_py}"}]}}))
    assert not doctor.check_hooks_exist()
    assert "SyntaxError" in capsys.readouterr().out

def test_permissions_consistency(fake_home, capsys):
    p = fake_home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    
    p.write_text(json.dumps({"permissions": {"allow": ["mcp__ai-router__nonexistent_tool"]}}))
    assert not doctor.check_permissions_consistency()
    assert "FAIL" in capsys.readouterr().out
    
    tools = doctor.get_server_tools()
    allowed = [f"mcp__ai-router__{t}" for t in tools]
    p.write_text(json.dumps({"permissions": {"allow": allowed}}))
    assert doctor.check_permissions_consistency()
    assert "OK" in capsys.readouterr().out

def test_vault_env(fake_home, monkeypatch, capsys):
    # AI_ROUTER_DATA_DIR overrides the VAULT root directly (matches
    # delegate.py's _vault_root() contract); the shared secrets dir stays
    # under agent-projects, resolved from AI_ROUTER_DOCTOR_HOME (fake_home)
    # via _get_home() since XDG_DATA_HOME is not set here.
    vault_dir = fake_home / "vault"
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("AI_ROUTER_DATA_DIR", str(vault_dir))

    f1 = fake_home / ".local" / "share" / "agent-projects" / "_shared" / "secrets" / ".env"
    f2 = vault_dir / "secrets" / ".env"
    f1.parent.mkdir(parents=True, exist_ok=True)
    f2.parent.mkdir(parents=True, exist_ok=True)
    
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    
    assert not doctor.check_vault_env()
    assert "FAIL" in capsys.readouterr().out
    
    f1.write_text("MINIMAX_API_KEY=dummy\nDEEPSEEK_API_KEY=dummy")
    f2.write_text("GROK_API_KEY=dummy")
    assert doctor.check_vault_env()
    assert "OK" in capsys.readouterr().out

def _isolate_vault(monkeypatch, tmp_path):
    """Point the rule-035 secrets lookup at an empty tree.

    Setting XDG_DATA_HOME / AI_ROUTER_DATA_DIR is NOT enough: delegate.py
    freezes AGENT_PROJECTS, VAULT and SECRETS_DIR into module-level constants
    at import time (delegate.py:191-199), so an env override applied after the
    import is silently ignored and load_env() keeps reading the real vault.
    Patch the constants themselves — otherwise these tests pass vacuously
    against the developer's live secrets.
    """
    vault = tmp_path / "vault"
    (vault / "secrets").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AI_ROUTER_DATA_DIR", str(vault))
    monkeypatch.setattr(delegate, "AGENT_PROJECTS", tmp_path / "xdg" / "agent-projects")
    monkeypatch.setattr(delegate, "VAULT", vault)
    monkeypatch.setattr(delegate, "SECRETS_DIR", vault / "secrets")
    return vault


def test_postgres_absent_everywhere_warns(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _isolate_vault(monkeypatch, tmp_path)
    # Neutralise the vault loader as well: this case asserts the "nothing
    # anywhere" branch. The resolver's own path handling is delegate.py's test,
    # not doctor's, and leaving it live let the real vault answer instead.
    monkeypatch.setattr(delegate, "load_env", lambda *a, **k: None)
    assert doctor.check_postgres()
    assert "WARN" in capsys.readouterr().out


def test_postgres_unreachable_warns_never_fails(monkeypatch, tmp_path, capsys):
    _isolate_vault(monkeypatch, tmp_path)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@127.0.0.1:1/db")
    assert doctor.check_postgres()
    out = capsys.readouterr().out
    assert "not reachable" in out
    assert "FAIL" not in out


def test_postgres_dsn_comes_from_the_vault(monkeypatch, tmp_path, capsys):
    """Regression: the DSN lives in the vault, not the ambient environment.

    Reading os.environ alone made this check report "not set" on a machine
    whose vault does define it — a check that could never fire. Port 1 is the
    discriminator: only the vault's DSN produces "not reachable" here, so a
    fall-through to the real (running) Postgres would fail this test.
    """
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    vault = _isolate_vault(monkeypatch, tmp_path)
    (vault / "secrets" / ".env").write_text(
        "POSTGRES_DSN=postgresql://u:p@127.0.0.1:1/db\n", encoding="utf-8")
    assert doctor.check_postgres()
    out = capsys.readouterr().out
    assert "not set" not in out
    assert "not reachable" in out


def test_launchd(capsys):
    res = doctor.check_launchd(labels=["com.fake.nonexistent.label.for.test"])
    assert res is True
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "FAIL" not in out


def test_repo_root_is_the_main_checkout_not_a_worktree():
    """--fix writes REPO_ROOT into the user's global ~/.claude.json.

    Derived from __file__ alone, a run from a throwaway git worktree would
    repoint the global MCP registration at a temp directory that is deleted
    minutes later. A linked worktree's `.git` is a FILE; only the main
    checkout's is a directory — so this assertion holds wherever the suite runs
    and fails the moment the resolution regresses to the current checkout.
    """
    assert (doctor.REPO_ROOT / ".git").is_dir()
    assert (doctor.REPO_ROOT / "mcp" / "server.py").is_file()
