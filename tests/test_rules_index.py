import os
import sys
from pathlib import Path

import pytest

# House style (see test_mcp_server.py): src/ goes on sys.path explicitly so
# the suite passes under `uv run pytest` from any invocation, not only
# `python -m pytest` (which silently adds the CWD).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import rules_index as ri  # noqa: E402
from rules_index import chunk_markdown  # noqa: E402

def test_chunk_markdown():
    text = """# Heading 1
Line 1
Line 2

# Heading 2
Line 3
"""
    chunks = chunk_markdown(text, max_tokens=10)
    assert len(chunks) == 2
    assert chunks[0]["heading"] == "# Heading 1"
    assert chunks[0]["start_line"] == 1
    assert "Line 1" in chunks[0]["text"]
    
    assert chunks[1]["heading"] == "# Heading 2"
    assert chunks[1]["start_line"] == 5

def test_output_cap(monkeypatch, capsys):
    class FakeArgs:
        query = "test"
        k = 5
        
    class FakeModel:
        def embed(self, texts, prefix=""):
            import numpy as np
            return np.array([[0.0] * 384])
            
    monkeypatch.setattr(ri, "E5Model", FakeModel)
    
    import psycopg
    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args, **kwargs): pass
        def fetchone(self): return None
        def fetchall(self):
            return [("path1.md", 1, "Heading", "a" * 3000) for _ in range(5)]
            
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return FakeCursor()
        
    monkeypatch.setattr(psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(os, "environ", {"POSTGRES_DSN": "dummy"})
    
    ri.cmd_search(FakeArgs())
    captured = capsys.readouterr()
    
    assert len(captured.out) < 10000
    assert len(captured.out) > 5000

def test_stale_index_warning(monkeypatch, capsys):
    class FakeArgs:
        query = "test"
        k = 5
        
    class FakeModel:
        def embed(self, texts, prefix=""):
            import numpy as np
            return np.array([[0.0] * 384])
            
    monkeypatch.setattr(ri, "E5Model", FakeModel)
    monkeypatch.setattr(ri, "project_info", lambda: ("ai-router", "newcommit"))
    
    import psycopg
    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args, **kwargs): pass
        def fetchone(self): return ("oldcommit",)
        def fetchall(self): return []
            
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return FakeCursor()
        
    monkeypatch.setattr(psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(os, "environ", {"POSTGRES_DSN": "dummy"})
    
    ri.cmd_search(FakeArgs())
    captured = capsys.readouterr()
    
    assert "Warning: rules index is stale. Index commit: oldcommit, Current commit: newcommit" in captured.err

# We can loosely check if huggingface cache for e5 exists
has_model = os.path.exists(os.path.expanduser("~/.cache/huggingface/hub"))


def _pg_available() -> bool:
    """Real probe, not a hardcoded False — on the dev machine (vault DSN +
    running container) the retrieval sanity test MUST actually run."""
    try:
        import psycopg
        ri.load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False
        psycopg.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False


has_pg = _pg_available()

@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_retrieval_sanity(monkeypatch):
    class Args:
        pass
    a = Args()
    ri.cmd_reindex(a)
    
    # k=5, membership assert: e5-small cross-lingual ranking (Persian query →
    # English rule text) is approximate; the contract is "the right rule is in
    # the top-k", not "top-1".
    a.query = "قانون کامیت"
    a.k = 5
    
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ri.cmd_search(a)
        
    output = out.getvalue()
    assert "040-git.md" in output
