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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import has_pg  # noqa: E402  (sits below the sys.path setup it needs)


def test_ingest_refuses_empty_rules_corpus(tmp_path, monkeypatch):
    """An empty rules corpus must abort before the first write.

    The GC at the end of `ingest()` deletes every indexed path that is not in
    the current corpus, so proceeding here would wipe the live rules index and
    leave a docs-only one that still answers queries. Refusing is the whole
    contract; assert it fires before anything touches Postgres by pointing at a
    DSN that cannot connect -- if the guard regresses, the test fails with a
    connection error instead of passing.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ri.ALLOW_NO_CONSTITUTION_ENV, raising=False)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://0.0.0.0:1/nope")
    monkeypatch.setattr(ri, "load_env", lambda: None)

    with pytest.raises(RuntimeError, match="rules corpus is empty"):
        ri.ingest()


@pytest.mark.skipif(
    not (has_pg and has_model),
    reason="Missing Postgres or the e5 model",
)
def test_retrieval_sanity(monkeypatch):
    """Retrieval returns the relevant rule for a Persian query.

    T-964: this used to reindex the LIVE .agent/constitution/rules symlink
    plus this repo's own docs/, both shared, mutable state:
      - the corpus grew every time someone edited docs/ARCHITECTURE.md,
        eventually pushing the target rule out of the top-k (a tripwire on
        unrelated doc edits, not a ranking regression);
      - every run wrote to the one fixed schema/repo namespace
        ("ai_router_test" / repo="ai-router"), so two concurrent pytest
        processes deleted each other's rows via ingest()'s own-corpus GC.
    Both are gone now: the corpus is a small fixture this test owns (see
    tests/fixtures/retrieval_sanity_corpus/), and each run gets its own
    `repo` namespace (rules_chunks/ingested_files are keyed by repo), so
    concurrent runs cannot see or delete each other's rows.
    """
    import uuid

    class Args:
        pass
    a = Args()

    assert "ai_router_test" in os.environ.get("POSTGRES_DSN", ""), (
        "Isolation failure: missing 'ai_router_test' schema in POSTGRES_DSN. "
        "Proceeding would reindex the LIVE rules index."
    )

    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "retrieval_sanity_corpus"
    assert (fixture_dir / ".agent" / "constitution" / "rules" / "040-git.md").exists(), (
        f"Missing test fixture corpus at {fixture_dir} -- checked-in fixture is gone."
    )

    # Give this run its own repo namespace: rules_chunks/ingested_files rows
    # are scoped by `repo`, and ingest()'s GC only ever deletes rows for the
    # SAME repo. A unique repo name per test invocation means two concurrent
    # runs of this test (or the whole suite) never see or delete each
    # other's rows even though they share the "ai_router_test" schema.
    repo_name = f"test-retrieval-sanity-{uuid.uuid4().hex}"
    monkeypatch.setattr(ri, "project_info", lambda: (repo_name, "fixture"))
    monkeypatch.chdir(fixture_dir)

    dsn = os.environ["POSTGRES_DSN"]
    try:
        # force=True: ingested_files' skip-if-unchanged check is keyed by
        # (collection, file_path) only -- NOT by repo -- so a PRIOR test run
        # (different repo_name, same fixture file, same content hash) would
        # otherwise make ingest() believe this file is already indexed and
        # skip writing any chunks for OUR repo namespace, leaving
        # cmd_search empty for this run.
        a.force = True
        ri.cmd_reindex(a)

        a.query = "قانون کامیت"
        a.k = 5

        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ri.cmd_search(a)

        output = out.getvalue()
        assert "040-git.md" in output
    finally:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM rules_chunks WHERE repo = %s", (repo_name,))
