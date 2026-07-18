import os
import sys
from pathlib import Path
import psycopg

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import code_index as ci
from rules_index import _get_tokenizer

def test_chunker_boundaries():
    source = b"""
class A:
    def method_1(self):
        pass

def func_outer():
    def func_inner():
        pass
    pass
"""
    tokenizer = _get_tokenizer()
    tree = ci.get_parser(ci.PY_LANG).parse(source)
    chunks = ci.chunk_node(tree.root_node, 'python', tokenizer, source)
    
    symbols = [c['symbol'] for c in chunks]
    assert "A" in symbols
    assert "A.method_1" in symbols
    assert "func_outer" in symbols
    assert "func_outer.func_inner" in symbols
    
    # method_1
    m1 = next(c for c in chunks if c['symbol'] == 'A.method_1')
    assert m1['start_line'] == 3
    assert m1['end_line'] == 4
    assert m1['parent_symbol'] == 'A'
    
    # func_outer
    f_o = next(c for c in chunks if c['symbol'] == 'func_outer')
    assert f_o['start_line'] == 6
    assert f_o['end_line'] == 9
    
def test_oversized_def_split():
    # To trigger oversized def (> 400 tokens), we'll artificially lower the limit
    # or create a huge string. Here we'll create a block of code with many statements.
    
    stmts = "\n    ".join([f"a_{i} = {i}" for i in range(150)])
    source = f"def big_func():\n    {stmts}".encode('utf-8')
    
    tokenizer = _get_tokenizer()
    tree = ci.get_parser(ci.PY_LANG).parse(source)
    chunks = ci.chunk_node(tree.root_node, 'python', tokenizer, source)
    
    assert len(chunks) > 1
    for c in chunks:
        assert c['symbol'] == "big_func"
        assert c['text'].startswith("def big_func():")

def test_call_graph_sanity():
    source = """
class A:
    def method_1(self):
        b_func()

def b_func():
    A.method_1()
    c()
"""
    calls = ci.extract_python_calls(source)
    assert ("A.method_1", "b_func") in calls
    assert ("b_func", "method_1") in calls
    assert ("b_func", "c") in calls

def test_output_cap(monkeypatch, capsys):
    class FakeArgs:
        query = "test"
        k = 5
        graph = False
        repo = ""
        
    class FakeModel:
        def embed(self, texts, prefix=""):
            import numpy as np
            return np.array([[0.0] * 384])
            
    monkeypatch.setattr(ci, "E5Model", FakeModel)
    
    import psycopg
    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args, **kwargs): pass
        def fetchone(self): return None
        def fetchall(self):
            return [(i, "path1.py", 1, 100, f"func_{i}", "a" * 3000) for i in range(5)]
            
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return FakeCursor()
        
    monkeypatch.setattr(psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(os, "environ", {"POSTGRES_DSN": "dummy"})
    
    ci.cmd_search(FakeArgs())
    captured = capsys.readouterr()
    
    assert len(captured.out) < 10000
    assert len(captured.out) > 5000

def test_stale_index_warning(monkeypatch, capsys):
    class FakeArgs:
        query = "test"
        k = 5
        graph = False
        repo = ""
        
    class FakeModel:
        def embed(self, texts, prefix=""):
            import numpy as np
            return np.array([[0.0] * 384])
            
    monkeypatch.setattr(ci, "E5Model", FakeModel)
    monkeypatch.setattr(ci, "project_info", lambda: ("ai-router", "newcommit"))
    
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
    
    ci.cmd_search(FakeArgs())
    captured = capsys.readouterr()
    
    assert "Warning: code index is stale. Index commit: oldcommit, Current commit: newcommit" in captured.err

has_model = os.path.exists(os.path.expanduser("~/.cache/huggingface/hub"))

def _pg_available() -> bool:
    try:
        import psycopg
        ci.load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False
        psycopg.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False

has_pg = _pg_available()

@pytest.mark.skipif(not (has_pg and has_model), reason="Missing Postgres or e5 model")
def test_incremental_reindex_mocked(monkeypatch, tmp_path):
    import subprocess

    class FakeArgs:
        rebuild = False

    class FakeCursor:
        def __init__(self):
            self.queries = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, args=None):
            self.queries.append((query, args))
        def fetchone(self):
            # return indexed_commit
            if self.queries[-1][0].startswith("SELECT repo_commit"):
                return ("oldcommit",)
            # return existing chunk match
            if self.queries[-1][0].startswith("SELECT id FROM code_chunks"):
                return None
            return (1,) # dummy id

    class FakeConn:
        def __init__(self):
            self.cur = FakeCursor()
            self.commits = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return self.cur
        def commit(self): self.commits += 1

    conn = FakeConn()
    monkeypatch.setattr(psycopg, "connect", lambda dsn: conn)
    monkeypatch.setenv("POSTGRES_DSN", "dummy")
    monkeypatch.setattr(ci, "project_info", lambda: ("ai-router", "newcommit"))
    monkeypatch.chdir(tmp_path)

    fake_file = tmp_path / "fake_file.py"
    fake_file.write_text("def f(): pass")

    # git diff is the only subprocess call left in cmd_reindex once the
    # chunker seam below is mocked
    def mock_run(*args, **kwargs):
        class Res:
            stdout = "fake_file.py\nvanished.py\n"
        return Res()
    monkeypatch.setattr(subprocess, "run", mock_run)

    monkeypatch.setattr(ci, "_chunk_files_subprocess", lambda paths: {
        str(fake_file.resolve()): [{
            "symbol": "f", "parent_symbol": None,
            "start_line": 1, "end_line": 1, "text": "def f(): pass",
        }],
    })

    class FakeVec(list):
        def tolist(self):
            return list(self)

    class FakeModel:
        def embed(self, texts, prefix=""):
            return [FakeVec([0.0] * 384) for _ in texts]
    monkeypatch.setattr(ci, "E5Model", FakeModel)

    ci.cmd_reindex(FakeArgs())

    queries = " ".join([q[0] for q in conn.cur.queries])
    assert "DELETE FROM code_chunks WHERE repo = %s AND path = ANY(%s)" in queries # vanished.py deleted
    assert "INSERT INTO code_chunks" in queries # fake_file.py inserted
    assert "DELETE FROM code_chunks WHERE repo = %s AND path = %s AND NOT" in queries # gc chunks in fake_file.py

def test_file_discovery_ignores_untracked(monkeypatch, tmp_path):
    import subprocess
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    
    subprocess.run(["git", "init"], cwd=repo_path, check=True)
    
    tracked_file = repo_path / "tracked.py"
    tracked_file.write_text("def tracked(): pass")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo_path, check=True)
    
    untracked_file = repo_path / "untracked.py"
    untracked_file.write_text("def untracked(): pass")
    
    class FakeArgs:
        rebuild = True
        
    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args, **kwargs): pass
        def fetchone(self): return (1,)
        
    class FakeConn:
        def __init__(self):
            self.cur = FakeCursor()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return self.cur
        def commit(self): pass

    conn = FakeConn()
    monkeypatch.setattr(psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(os, "environ", {"POSTGRES_DSN": "dummy"})
    monkeypatch.setattr(ci, "project_info", lambda: ("ai-router", "newcommit"))
    monkeypatch.setattr(Path, "cwd", lambda: repo_path)
    
    read_files = []
    original_read_text = Path.read_text
    def mock_read_text(self, *args, **kwargs):
        read_files.append(self.name)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mock_read_text)
    monkeypatch.setattr(ci, "_chunk_files_subprocess", lambda paths: {})
    monkeypatch.setattr(ci, "E5Model", lambda: None)

    ci.cmd_reindex(FakeArgs())
    
    assert "tracked.py" in read_files
    assert "untracked.py" not in read_files
