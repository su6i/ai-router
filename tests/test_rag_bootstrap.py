"""Tests for scripts/rag_bootstrap.py"""
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_index
import delegate as d
import rag_bootstrap
import rules_index
import sessions_index
import skills_index


def test_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag_bootstrap.py", "--dry-run"])

    def mock_connect(*args, **kwargs):
        raise AssertionError("should not connect")

    monkeypatch.setattr("psycopg.connect", mock_connect)

    assert rag_bootstrap.main() == 0
    captured = capsys.readouterr()
    assert "rules" in captured.out
    assert "skills" in captured.out
    assert "sessions" in captured.out
    assert "code" in captured.out


def test_no_dsn(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag_bootstrap.py"])
    # main() calls d.load_env(), which re-reads the machine's real vault
    # .env and repopulates os.environ unconditionally — on any machine that
    # actually has the vault configured (this one included), that silently
    # undoes delenv() below and the "no DSN" case would never be exercised.
    # Stub load_env() to a no-op so the test controls POSTGRES_DSN, not the
    # real secrets file.
    monkeypatch.setattr(d, "load_env", lambda: None)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    assert rag_bootstrap.main() == 2
    captured = capsys.readouterr()
    assert captured.err != ""


def test_db_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag_bootstrap.py"])
    monkeypatch.setattr(d, "load_env", lambda: None)  # see test_no_dsn comment
    monkeypatch.setenv("POSTGRES_DSN", "fake_dsn")

    def mock_connect(*args, **kwargs):
        raise psycopg.OperationalError("fake")

    monkeypatch.setattr("psycopg.connect", mock_connect)

    assert rag_bootstrap.main() == 2
    captured = capsys.readouterr()
    assert captured.err != ""


def test_success(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag_bootstrap.py"])
    monkeypatch.setattr(d, "load_env", lambda: None)  # see test_no_dsn comment
    monkeypatch.setenv("POSTGRES_DSN", "fake_dsn")

    class StubConnection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    conn = StubConnection()

    def mock_connect(*args, **kwargs):
        return conn

    monkeypatch.setattr("psycopg.connect", mock_connect)

    called = []

    def mock_init_rules(c):
        called.append("rules")

    def mock_init_skills(c):
        called.append("skills")

    def mock_init_sessions(c):
        called.append("sessions")

    def mock_init_code(c):
        called.append("code")

    monkeypatch.setattr(rules_index, "init_db", mock_init_rules)
    monkeypatch.setattr(skills_index, "init_db", mock_init_skills)
    monkeypatch.setattr(sessions_index, "init_db", mock_init_sessions)
    monkeypatch.setattr(code_index, "init_db", mock_init_code)

    assert rag_bootstrap.main() == 0
    assert called == ["rules", "skills", "sessions", "code"]
    assert conn.closed is True
