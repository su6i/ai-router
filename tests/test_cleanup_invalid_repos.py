import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import cleanup_invalid_repos as cir  # noqa: E402
import sessions_index as si  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import has_pg  # noqa: E402  (sits below the sys.path setup it needs)

requires_pg = pytest.mark.skipif(not has_pg, reason="Missing Postgres")


@requires_pg
def test_cleanup_deletes_only_invalid_repo(monkeypatch, tmp_path):
    dsn = os.environ.get("POSTGRES_DSN")

    agent_root = tmp_path / "agent-projects"
    real_repo_dir = agent_root / "real-repo"
    real_repo_dir.mkdir(parents=True)
    monkeypatch.setattr(cir, "_agent_projects_root", lambda: agent_root)
    # No code repos configured for this test -- code/skill/rules tables are
    # left untouched by the fixture rows below, so an empty root list is fine.
    monkeypatch.setattr(cir, "get_repo_roots", lambda: [])

    with psycopg.connect(dsn) as conn:
        si.init_db(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_chunks (repo, path, chunk, chunk_sha) VALUES (%s, %s, %s, %s)",
                ("real-repo", "p1.md", "valid row", "sha-valid"),
            )
            cur.execute(
                "INSERT INTO session_chunks (repo, path, chunk, chunk_sha) VALUES (%s, %s, %s, %s)",
                ("--fake-flag", "p2.md", "phantom row", "sha-phantom"),
            )
        conn.commit()

    report = cir.cleanup_invalid_repos(dsn)

    assert "--fake-flag" in report["session_chunks"]["deleted"]
    assert "real-repo" in report["session_chunks"]["kept"]
    assert "--fake-flag" not in report["session_chunks"]["kept"]

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_chunks WHERE repo = %s", ("--fake-flag",))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM session_chunks WHERE repo = %s", ("real-repo",))
            assert cur.fetchone()[0] == 1


@requires_pg
def test_cleanup_is_idempotent(monkeypatch, tmp_path):
    dsn = os.environ.get("POSTGRES_DSN")

    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir(parents=True)
    monkeypatch.setattr(cir, "_agent_projects_root", lambda: agent_root)
    monkeypatch.setattr(cir, "get_repo_roots", lambda: [])

    with psycopg.connect(dsn) as conn:
        si.init_db(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_chunks (repo, path, chunk, chunk_sha) VALUES (%s, %s, %s, %s)",
                ("--idempotent-junk", "p3.md", "phantom row", "sha-idem"),
            )
        conn.commit()

    report1 = cir.cleanup_invalid_repos(dsn)
    assert "--idempotent-junk" in report1["session_chunks"]["deleted"]

    report2 = cir.cleanup_invalid_repos(dsn)
    assert "--idempotent-junk" not in report2["session_chunks"]["deleted"]
    assert "--idempotent-junk" not in report2["session_chunks"]["kept"]


@requires_pg
def test_cleanup_dry_run_deletes_nothing(monkeypatch, tmp_path):
    dsn = os.environ.get("POSTGRES_DSN")

    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir(parents=True)
    monkeypatch.setattr(cir, "_agent_projects_root", lambda: agent_root)
    monkeypatch.setattr(cir, "get_repo_roots", lambda: [])

    with psycopg.connect(dsn) as conn:
        si.init_db(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_chunks (repo, path, chunk, chunk_sha) VALUES (%s, %s, %s, %s)",
                ("--dry-run-junk", "p4.md", "phantom row", "sha-dry"),
            )
        conn.commit()

    report = cir.cleanup_invalid_repos(dsn, dry_run=True)
    assert "--dry-run-junk" in report["session_chunks"]["deleted"]

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_chunks WHERE repo = %s", ("--dry-run-junk",))
            assert cur.fetchone()[0] == 1
