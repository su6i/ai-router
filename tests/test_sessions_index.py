import os
import sys
import json
import subprocess
from pathlib import Path
import psycopg
import pytest
import re

# Setup path so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import sessions_index as si  # noqa: E402
from rules_index import chunk_markdown  # noqa: E402

def test_chunker_with_dates():
    text = """## 2026-07-21 session
تست متن فارسی.
این یک تست است.

### 2026-07-22 test
Another test block.

## No date here
Just a normal heading.
"""
    chunks = chunk_markdown(text)
    
    assert len(chunks) == 3
    
    c1 = chunks[0]
    assert c1["heading"] == "## 2026-07-21 session"
    assert "تست متن فارسی" in c1["text"]
    
    c2 = chunks[1]
    assert c2["heading"] == "### 2026-07-22 test"
    assert "Another test block." in c2["text"]
    
    c3 = chunks[2]
    assert c3["heading"] == "## No date here"
    assert "Just a normal heading." in c3["text"]
    
    import re
    # Emulate the date regex extraction in ingest
    dates = []
    for c in chunks:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', c["heading"])
        dates.append(m.group(1) if m else None)
        
    assert dates == ["2026-07-21", "2026-07-22", None]

has_model = os.path.exists(os.path.expanduser("~/.cache/huggingface/hub"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import has_pg  # noqa: E402  (sits below the sys.path setup it needs)

@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_ingest_idempotency(monkeypatch, tmp_path):
    class Args:
        pass
    a = Args()
    
    # Create fake vault structure
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()
    pdir = agent_root / "test-repo"
    (pdir / "workspace").mkdir(parents=True)
    sess_file = pdir / "workspace" / "SESSION.md"
    sess_file.write_text("## 2026-07-21\nSome test text for idempotency.\n")
    
    # Patch vault root
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    
    dsn = os.environ.get("POSTGRES_DSN")
    
    # First ingest
    si.cmd_reindex(a)
    
    # Count rows
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM session_chunks WHERE repo = 'test-repo'")
            count1 = cur.fetchone()[0]
            
    assert count1 > 0
            
    # Second ingest
    si.cmd_reindex(a)
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM session_chunks WHERE repo = 'test-repo'")
            count2 = cur.fetchone()[0]
            
    assert count2 == count1, "Reindexing inserted new rows unexpectedly"


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_mcp_sessions_collection(monkeypatch, tmp_path):
    # Ensure there's something to search
    class Args:
        pass
    a = Args()
    
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()
    pdir = agent_root / "test-repo-mcp"
    (pdir / "workspace").mkdir(parents=True)
    sess_file = pdir / "workspace" / "SESSION.md"
    sess_file.write_text("## 2026-07-21\nUniqueMcpSearchTerm.\n")
    
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    si.cmd_reindex(a)

    # Spawn MCP server subprocess
    server_path = Path(__file__).resolve().parent.parent / "mcp" / "server.py"
    
    p = subprocess.Popen(
        [sys.executable, str(server_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Send init
    req1 = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    p.stdin.write(json.dumps(req1) + "\n")
    p.stdin.flush()
    resp1 = json.loads(p.stdout.readline())
    assert resp1["id"] == 1
    
    # Send rules_lookup with collection="sessions"
    req2 = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "rules_lookup",
            "arguments": {
                "query": "UniqueMcpSearchTerm",
                "collection": "sessions"
            }
        }
    }
    p.stdin.write(json.dumps(req2) + "\n")
    p.stdin.flush()
    
    resp2 = json.loads(p.stdout.readline())
    p.terminate()
    
    assert "result" in resp2
    assert "UniqueMcpSearchTerm" in resp2["result"]["content"][0]["text"]


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_extended_sessions_and_incremental(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()
    
    # 1. repo SESSION.md
    r1 = agent_root / "repo1" / "workspace"
    r1.mkdir(parents=True)
    (r1 / "SESSION.md").write_text("## 2026-07-01\nRepo1 session text.\n")
    
    # 2. _memory/sessions
    mem_sess = agent_root / "_memory" / "sessions"
    mem_sess.mkdir(parents=True)
    (mem_sess / "2026-06-02-research.md").write_text("## Research Digest\nMemory session content.\n")
    
    # 3. _memory/handoffs
    mem_hand = agent_root / "_memory" / "handoffs"
    mem_hand.mkdir(parents=True)
    (mem_hand / "HANDOFF-2026-07-05.md").write_text("## Handoff Note\nHandoff note content.\n")
    (mem_hand / "backup.jsonl").write_text('{"type": "raw_jsonl_backup"}\n')  # Should be skipped
    
    # 4. per-repo archive
    arch_dir = agent_root / "repo2" / "workspace" / "archive"
    arch_dir.mkdir(parents=True)
    (arch_dir / "old_session.md").write_text("## 2026-05-01\nArchived session.\n")
    
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    
    # First ingest
    res1 = si.ingest(force=False)
    assert res1["files_seen"] == 4
    assert res1["chunks_written"] >= 4
    assert res1["skipped"] == 0
    
    # Second incremental ingest
    res2 = si.ingest(force=False)
    assert res2["skipped"] == 4
    assert res2["files_seen"] == 0
    assert res2["chunks_written"] == 0


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_sessions_deletion_drops_chunks(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()
    
    r1 = agent_root / "delrepo" / "workspace"
    r1.mkdir(parents=True)
    f1 = r1 / "SESSION.md"
    f1.write_text("## Session Del 1\nSome text.\n")
    
    mem_sess = agent_root / "_memory" / "sessions"
    mem_sess.mkdir(parents=True)
    f2 = mem_sess / "2026-06-01-temp.md"
    f2.write_text("## Session Del 2\nTemporary session content.\n")
    
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    
    res1 = si.ingest(force=True)
    assert res1["files_seen"] == 2
    
    # Delete f2 from disk
    f2.unlink()
    
    res2 = si.ingest(force=False)
    assert res2["chunks_deleted"] > 0


def test_receipt_db_down(monkeypatch, tmp_path, capsys):
    import rag_ingest
    
    fake_file = tmp_path / "test_sess.md"
    fake_file.write_text("## Test\nContent\n")
    
    def mock_connect(*args, **kwargs):
        raise psycopg.OperationalError("DB connection failed")
        
    monkeypatch.setattr(psycopg, "connect", mock_connect)
    
    with pytest.raises(SystemExit) as exc_info:
        rag_ingest.handle_receipt(str(fake_file))
        
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "RECEIPT" not in captured.out
    assert captured.out == ""


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_receipt_success(monkeypatch, tmp_path, capsys):
    import rag_ingest
    
    agent_root = tmp_path / "agent-projects"
    r1 = agent_root / "receipt_repo" / "workspace"
    r1.mkdir(parents=True)
    f1 = r1 / "SESSION.md"
    f1.write_text("## Receipt Test Heading\nTesting receipt generation from DB.\n")
    
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    monkeypatch.setattr(rag_ingest.d, "_agent_projects_root", lambda: agent_root)
    
    rag_ingest.handle_receipt(str(f1))
    
    captured = capsys.readouterr()
    assert "RECEIPT col:sessions sha:" in captured.out
    assert "path:receipt_repo/workspace/SESSION.md" in captured.out


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_sessions_modification_reingests(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()
    
    r1 = agent_root / "modrepo" / "workspace"
    r1.mkdir(parents=True)
    f1 = r1 / "SESSION.md"
    f1.write_text("## Original Heading\nOriginal text content.\n")
    
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    
    res1 = si.ingest(force=False)
    assert res1["files_seen"] == 1
    assert res1["chunks_written"] == 1
    
    # Modify f1 content
    f1.write_text("## Modified Heading\nModified text content.\n")
    
    res2 = si.ingest(force=False)
    assert res2["skipped"] == 0
    assert res2["files_seen"] == 1
    assert res2["chunks_written"] == 1


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_sweep_does_not_evict_hook_ingested_file(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()
    
    r1 = agent_root / "hook_repo" / "workspace" / "inbox"
    r1.mkdir(parents=True)
    note_md_path = r1 / "note.md"
    note_md_path.write_text("## Note\nHook ingested text.\n")
    
    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)
    
    si.ingest(force=True, target_file=note_md_path)
    
    dsn = os.environ.get("POSTGRES_DSN")
    rel_path = str(note_md_path.relative_to(agent_root))
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM session_chunks WHERE path = %s", (rel_path,))
            count1 = cur.fetchone()[0]
            
    assert count1 > 0
    
    si.ingest(force=False)
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM session_chunks WHERE path = %s", (rel_path,))
            count2 = cur.fetchone()[0]
            
    assert count2 == count1
    assert count2 > 0


def test_extract_date_normalisation():
    # 1. Heading with Persian-Indic full-date digits normalizes to ISO
    res1 = si._extract_date("## ۲۰۲۶-۰۹-۰۵ session note", "", "test.md")
    assert res1 == "2026-09-05"

    # 2. Heading with Jalali date converts to Gregorian ISO equivalent
    res2 = si._extract_date("## ۱۴۰۵-۰۴-۰۱ jalaali session", "", "test.md")
    assert res2 == "2026-06-22"

    # 3. Heading with garbage numbers that don't form a valid date falls through to None
    res3 = si._extract_date("## 9999-99-99 invalid date", "", "test.md")
    assert res3 is None

    # Fall-through to next real match in cascade (e.g. text date:)
    res4 = si._extract_date("## 9999-99-99 invalid date", "date: 2026-08-15\ncontent", "test.md")
    assert res4 == "2026-08-15"


@pytest.mark.skipif(not has_pg, reason="Missing Postgres")
def test_cmd_backfill_dates(capsys):
    dsn = os.environ.get("POSTGRES_DSN")
    with psycopg.connect(dsn) as conn:
        si.init_db(conn)
        with conn.cursor() as cur:
            # Insert fixtures with Persian-digit, Jalali, and existing ISO dates
            cur.execute(
                "INSERT INTO session_chunks (repo, path, heading, start_line, chunk, chunk_sha, date, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector) RETURNING id",
                ("repo-bf", "p1.md", "## Heading", 1, "test text 1", "sha1", "۲۰۲۶-۰۹-۰۵", str([0.0] * 384))
            )
            id1 = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO session_chunks (repo, path, heading, start_line, chunk, chunk_sha, date, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector) RETURNING id",
                ("repo-bf", "p2.md", "## Heading", 1, "test text 2", "sha2", "۱۴۰۵-۰۴-۰۱", str([0.0] * 384))
            )
            id2 = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO session_chunks (repo, path, heading, start_line, chunk, chunk_sha, date, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector) RETURNING id",
                ("repo-bf2", "p3.md", "## Heading", 1, "test text 3", "sha3", "2026-09-05", str([0.0] * 384))
            )
            id3 = cur.fetchone()[0]
        conn.commit()

    class Args:
        pass

    # Run first time
    si.cmd_backfill_dates(Args())
    captured = capsys.readouterr()
    assert "repo-bf: 2 rows changed" in captured.out

    # Check updated values
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, date FROM session_chunks WHERE id IN (%s, %s, %s)", (id1, id2, id3))
            row_dict = dict(cur.fetchall())
            assert row_dict[id1] == "2026-09-05"
            assert row_dict[id2] == "2026-06-22"
            assert row_dict[id3] == "2026-09-05"

    # Run second time (idempotency)
    si.cmd_backfill_dates(Args())
    captured2 = capsys.readouterr()
    assert "No rows changed." in captured2.out

    # Regression guard across ALL rows in session_chunks
    iso_date_re = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT date FROM session_chunks WHERE date IS NOT NULL")
            all_dates = [r[0] for r in cur.fetchall()]
            for d in all_dates:
                assert iso_date_re.match(d) is not None, f"Non-ISO date found in session_chunks: {d}"


@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_reject_flag_like_repo_name(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent-projects"
    agent_root.mkdir()

    bad_repo = agent_root / "--bad-flag" / "workspace"
    bad_repo.mkdir(parents=True)
    (bad_repo / "SESSION.md").write_text("## Session\nBad flag session text.\n")

    good_repo = agent_root / "good-repo" / "workspace"
    good_repo.mkdir(parents=True)
    (good_repo / "SESSION.md").write_text("## Session\nGood session text.\n")

    monkeypatch.setattr(si, "_agent_projects_root", lambda: agent_root)

    res = si.ingest(force=True)
    assert res["invalid_repo"] == 1
    assert res["files_seen"] == 1

    if has_pg:
        dsn = os.environ.get("POSTGRES_DSN")
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM session_chunks WHERE repo = '--bad-flag'")
                assert cur.fetchone()[0] == 0



