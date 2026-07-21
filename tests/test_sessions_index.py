import os
import sys
import json
import subprocess
from pathlib import Path
import psycopg
import pytest

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

def _pg_available() -> bool:
    try:
        si.load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False
        psycopg.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False

has_pg = _pg_available()

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
