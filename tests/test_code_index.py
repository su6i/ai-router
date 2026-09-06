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
            
    monkeypatch.setattr(ci, "get_model", FakeModel)
    
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
            
    monkeypatch.setattr(ci, "get_model", FakeModel)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import has_pg  # noqa: E402  (sits below the sys.path setup it needs)

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
        def fetchall(self):
            return [(1,)]

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
    monkeypatch.setattr(ci, "get_model", FakeModel)

    ci.cmd_reindex(FakeArgs())

    queries = " ".join([q[0] for q in conn.cur.queries])
    assert "DELETE FROM code_chunks WHERE repo = %s AND path = ANY(%s) RETURNING id" in queries # vanished.py deleted
    assert "INSERT INTO code_chunks" in queries # fake_file.py inserted
    assert "DELETE FROM code_chunks WHERE repo = %s AND path = %s AND NOT (chunk_hash = ANY(%s)) RETURNING id" in queries # gc chunks in fake_file.py

def test_ingested_key_prefixes_repo_name():
    assert ci._ingested_key("myrepo", "src/__init__.py") == "myrepo::src/__init__.py"
    assert ci._ingested_key("other-repo", "src/__init__.py") == "other-repo::src/__init__.py"


def test_ingested_files_gc_only_touches_this_repos_keys(monkeypatch, tmp_path):
    # T-953 follow-up: ingested_files has no repo column, so a --force
    # rebuild's GC step used to do `NOT (file_path = ANY(this_repo_paths))`
    # with a bare relative path -- which deletes every OTHER repo's rows
    # too, since none of their paths are in "this repo's paths" either.
    # After the fix, the GC step must only ever touch keys carrying this
    # repo's own "<repo>::" prefix.
    import subprocess

    repo_path = tmp_path / "myrepo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)

    tracked = repo_path / "tracked.py"
    tracked.write_text("def f(): pass")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo_path, check=True, capture_output=True)

    class FakeCursor:
        def __init__(self, store):
            self.store = store
            self.queries = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, args=None):
            self.queries.append((query, args))
        def fetchone(self):
            q = self.queries[-1][0]
            if q.startswith("SELECT repo_commit"):
                return None  # force path, but exercised regardless
            if q.startswith("SELECT id FROM code_chunks"):
                return None  # no existing chunk row -> insert path
            return (1,)  # dummy id for INSERT ... RETURNING id
        def fetchall(self):
            q = self.queries[-1][0]
            if q.startswith("SELECT file_path FROM ingested_files"):
                # Pre-existing keys from an unrelated repo AND a stale key
                # belonging to THIS repo that should get GC'd.
                return [("otherrepo::foo.py",), ("myrepo::stale_old_file.py",)]
            return []

    class FakeConn:
        def __init__(self):
            self.cur = FakeCursor(self)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return self.cur
        def commit(self): pass

    conn = FakeConn()
    monkeypatch.setattr(psycopg, "connect", lambda dsn: conn)
    monkeypatch.setenv("POSTGRES_DSN", "dummy")
    monkeypatch.setattr(ci, "_project_info_for", lambda p: ("myrepo", "abc123"))

    monkeypatch.setattr(ci, "_chunk_files_subprocess", lambda paths: {
        str(tracked.resolve()): [{
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
    monkeypatch.setattr(ci, "get_model", FakeModel)

    ci.ingest(force=True, repo_path=repo_path)

    insert_calls = [a for q, a in conn.cur.queries if q.startswith("INSERT INTO ingested_files")]
    assert insert_calls, "expected an ingested_files upsert"
    assert insert_calls[0][0] == "myrepo::tracked.py"

    gc_deletes = [a for q, a in conn.cur.queries
                  if q.startswith("DELETE FROM ingested_files") and "ANY" in q]
    assert gc_deletes, "expected a scoped GC delete"
    deleted_keys = gc_deletes[0][0]
    assert "myrepo::stale_old_file.py" in deleted_keys
    assert "otherrepo::foo.py" not in deleted_keys


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
        def fetchall(self): return []
        
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
    monkeypatch.setattr(ci, "get_model", lambda: None)

    ci.cmd_reindex(FakeArgs())
    
    assert "tracked.py" in read_files
    assert "untracked.py" not in read_files


def test_get_repo_roots_config_override(monkeypatch, tmp_path):
    import json
    import delegate

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(delegate, "DATA_DIR", data_dir)

    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    repo_a.mkdir()
    repo_b.mkdir()

    cfg_file = data_dir / "code_repo_roots.json"
    cfg_file.write_text(
        json.dumps({
            "roots": [
                str(repo_a),
                str(repo_b),
                str(tmp_path / "does_not_exist"),
            ]
        }),
        encoding="utf-8",
    )

    roots = ci.get_repo_roots()
    assert roots == [repo_a, repo_b]
    assert (tmp_path / "does_not_exist") not in roots


def test_get_repo_roots_default_scans_home_github(monkeypatch, tmp_path):
    import delegate

    empty_data_dir = tmp_path / "empty_data"
    empty_data_dir.mkdir()
    monkeypatch.setattr(delegate, "DATA_DIR", empty_data_dir)
    monkeypatch.setattr(ci.Path, "home", lambda: tmp_path)

    github_dir = tmp_path / "@-github"
    repo1 = github_dir / "repo1"
    (repo1 / ".git").mkdir(parents=True)
    not_a_repo = github_dir / "not_a_repo"
    not_a_repo.mkdir(parents=True)
    repo2 = github_dir / "repo2"
    (repo2 / ".git").mkdir(parents=True)

    roots = ci.get_repo_roots()
    assert roots == [repo1, repo2]
    assert not_a_repo not in roots


def test_lang_for_path_covers_wo_extensions():
    # T-953 scope: .py .js .ts .tsx .jsx .sh .go .rs
    assert ci._lang_for_path("a.py") == "python"
    assert ci._lang_for_path("a.sh") == "bash"
    assert ci._lang_for_path("a.js") == "javascript"
    assert ci._lang_for_path("a.jsx") == "javascript"
    assert ci._lang_for_path("a.ts") == "typescript"
    assert ci._lang_for_path("a.tsx") == "tsx"
    assert ci._lang_for_path("a.go") == "go"
    assert ci._lang_for_path("a.rs") == "rust"
    assert ci._lang_for_path("a.md") is None
    assert ci._lang_for_path("a.png") is None


def test_chunk_generic_splits_large_file_and_has_no_symbol():
    lines = [f"const x_{i} = {i};" for i in range(300)]
    source = "\n".join(lines).encode("utf-8")

    chunks = ci.chunk_generic(source)

    assert len(chunks) > 1
    for c in chunks:
        assert c["symbol"] is None
        assert c["parent_symbol"] is None
    # every original line shows up in some chunk, in order, none dropped
    rebuilt = "\n".join(c["text"] for c in chunks)
    assert rebuilt.count("const x_0 = 0;") == 1
    assert rebuilt.count("const x_299 = 299;") == 1


def test_chunk_generic_empty_file_yields_no_chunks():
    assert ci.chunk_generic(b"") == []


def test_cmd_chunk_files_ts_file_uses_generic_chunker(tmp_path, capsys):
    ts_file = tmp_path / "component.tsx"
    ts_file.write_text("export function Foo() { return 1; }\n")

    ci.cmd_chunk_files([str(ts_file)])
    captured = capsys.readouterr()
    import json as _json
    out = _json.loads(captured.out)

    chunks = out[str(ts_file)]
    assert len(chunks) == 1
    assert chunks[0]["symbol"] is None
    assert "export function Foo" in chunks[0]["text"]


def test_cmd_chunk_files_python_script_with_no_defs_falls_back_to_generic(tmp_path, capsys):
    # T-953 follow-up: a real top-level script (no def/class at all) must
    # not silently contribute 0 chunks -- this is exactly what was
    # swallowing polycast's experiments/gemini/scripts/*.py.
    py_file = tmp_path / "finalize_eval.py"
    py_file.write_text(
        "import json\n"
        "with open('x') as f:\n"
        "    data = json.load(f)\n"
        "print(data)\n"
    )

    ci.cmd_chunk_files([str(py_file)])
    captured = capsys.readouterr()
    import json as _json
    out = _json.loads(captured.out)

    chunks = out[str(py_file)]
    assert len(chunks) == 1
    assert chunks[0]["symbol"] is None
    assert "import json" in chunks[0]["text"]


def test_cmd_chunk_files_empty_python_file_yields_no_chunks(tmp_path, capsys):
    # Only a file with 0 bytes of real content stays at 0 chunks.
    py_file = tmp_path / "__init__.py"
    py_file.write_text("")

    ci.cmd_chunk_files([str(py_file)])
    captured = capsys.readouterr()
    import json as _json
    out = _json.loads(captured.out)

    assert out[str(py_file)] == []


def test_cmd_chunk_files_python_file_with_real_def_unaffected(tmp_path, capsys):
    # A file that DOES have a def/class still gets normal AST chunking,
    # not the generic fallback -- the fallback only fires when the AST
    # walk finds zero chunks.
    py_file = tmp_path / "has_func.py"
    py_file.write_text("def f():\n    return 1\n")

    ci.cmd_chunk_files([str(py_file)])
    captured = capsys.readouterr()
    import json as _json
    out = _json.loads(captured.out)

    chunks = out[str(py_file)]
    assert len(chunks) == 1
    assert chunks[0]["symbol"] == "f"


def test_project_info_for_uses_directory_basename_not_remote(monkeypatch, tmp_path):
    # T-953 defect: two checkouts can share a git remote (e.g. a "-test"
    # fork whose origin was never repointed). Identity must come from the
    # directory, which get_repo_roots() already guarantees is unique among
    # swept roots -- not from the remote URL, which is not guaranteed
    # unique and previously caused parsi-rtl-test's chunks to collide with
    # (and be GC'd away by) parsi-rtl's.
    import subprocess

    repo_a = tmp_path / "parsi-rtl"
    repo_b = tmp_path / "parsi-rtl-test"
    for repo in (repo_a, repo_b):
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/su6i/parsi-rtl.git"],
            cwd=repo, check=True, capture_output=True,
        )

    name_a, _ = ci._project_info_for(repo_a)
    name_b, _ = ci._project_info_for(repo_b)

    assert name_a == "parsi-rtl"
    assert name_b == "parsi-rtl-test"
    assert name_a != name_b


def test_project_info_for_matches_directory_casing(tmp_path):
    # T-953 hint: a directory named "Arix" must not be identified as the
    # lowercase "arix" the git remote URL happens to use.
    import subprocess

    repo = tmp_path / "Arix"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/su6i/arix.git"],
        cwd=repo, check=True, capture_output=True,
    )

    name, _ = ci._project_info_for(repo)
    assert name == "Arix"


def test_ingest_broken_git_repo_logs_and_returns_cleanly(monkeypatch, tmp_path, capsys):
    # T-953 DoD #5 names "no git" as a failure mode the sweep must isolate.
    # A directory whose .git is garbage (not a real repo) must not raise --
    # it should log to stderr (previously silent) and come back with 0
    # files, so sweep() never even needs its own except-block for this case.
    broken = tmp_path / "broken-repo"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git" / "HEAD").write_text("not a real git repo")

    monkeypatch.setattr(ci, "_project_info_for", lambda p: ("broken-repo", None))
    monkeypatch.setenv("POSTGRES_DSN", "dummy")

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, args=None):
            self.last_query = query
        def fetchone(self):
            if self.last_query.startswith("SELECT repo_commit"):
                return None
            return (0,)  # count(*) queries
        def fetchall(self): return []

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return FakeCursor()
        def commit(self): pass

    monkeypatch.setattr(psycopg, "connect", lambda dsn: FakeConn())

    stats = ci.ingest(force=False, repo_path=broken)

    captured = capsys.readouterr()
    assert "is not readable as a git repo, skipping" in captured.err
    assert stats["files_seen"] == 0


def test_is_excluded():
    excluded_paths = [
        "node_modules/foo.js",
        "a/.venv/b.py",
        "venv/x.py",
        "dist/bundle.js",
        "build/out.py",
        "__pycache__/x.pyc",
        "vendor/.git/config",
        "lib/jquery.min.js",
    ]
    for path_str in excluded_paths:
        assert ci._is_excluded(path_str) is True, f"Expected {path_str} to be excluded"

    included_paths = [
        "src/code_index.py",
        "tests/test_foo.py",
        "README.md",
    ]
    for path_str in included_paths:
        assert ci._is_excluded(path_str) is False, f"Expected {path_str} not to be excluded"


def test_sweep_one_repo_failure_does_not_abort_others(monkeypatch):
    repo_a = Path("/fake/repoA")
    repo_b = Path("/fake/repoB")
    repo_c = Path("/fake/repoC")
    roots = [repo_a, repo_b, repo_c]

    monkeypatch.setattr(ci, "get_repo_roots", lambda: roots)

    def mock_ingest(force=False, repo_path=None):
        if repo_path == repo_b:
            raise RuntimeError("boom")
        return {"files_seen": 1, "chunks_written": 1, "chunks_deleted": 0, "skipped": 0}

    monkeypatch.setattr(ci, "ingest", mock_ingest)
    monkeypatch.setattr(ci, "load_env", lambda: None)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    result = ci.sweep(force=False)

    assert result["repos_seen"] == 3
    assert result["repos_failed"] == 1
    assert "boom" in result["failures"][str(repo_b)]
    assert result["files_seen"] == 2
    assert result["chunks_written"] == 2


def test_sweep_reraises_on_postgres_down(monkeypatch):
    monkeypatch.setattr(ci, "get_repo_roots", lambda: [Path("/fake/repoA")])

    def mock_ingest(force=False, repo_path=None):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(ci, "ingest", mock_ingest)
    monkeypatch.setattr(ci, "load_env", lambda: None)

    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        ci.sweep(force=False)


def test_sweep_budget_and_resume(monkeypatch):
    import json
    import delegate

    roots = [
        Path("/fake/repo0"),
        Path("/fake/repo1"),
        Path("/fake/repo2"),
        Path("/fake/repo3"),
    ]
    monkeypatch.setattr(ci, "get_repo_roots", lambda: roots)

    called_repos = []

    def mock_ingest(force=False, repo_path=None):
        called_repos.append(repo_path)
        return {}

    monkeypatch.setattr(ci, "ingest", mock_ingest)
    monkeypatch.setattr(ci, "load_env", lambda: None)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    # Budget of -1 ensures immediate budget trip before any repo is processed
    ci.sweep(force=False, budget_seconds=-1)

    assert called_repos == []
    state_file = delegate.DATA_DIR / "code_sweep_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text("utf-8"))
    assert state.get("next_index") == 0

    # Normal budget runs all 4 repos
    ci.sweep(force=False, budget_seconds=1500)

    assert called_repos == roots
    state = json.loads(state_file.read_text("utf-8"))
    assert state.get("next_index") == 0


def test_all_repos_search_labels_each_hit_by_repo(monkeypatch, capsys):
    class FakeArgs:
        query = "test"
        k = 5
        graph = False
        repo = ""
        all_repos = True

    class FakeModel:
        def embed(self, texts, prefix=""):
            import numpy as np
            return np.array([[0.0] * 384])

    monkeypatch.setattr(ci, "get_model", FakeModel)

    rows = [
        (1, "a.py", 1, 5, "f1", "short chunk text", "ai-router"),
        (2, "b.py", 1, 5, "f2", "short chunk text", "Arix"),
    ]

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            return None

        def fetchall(self):
            return rows

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(os, "environ", {"POSTGRES_DSN": "dummy"})

    ci.cmd_search(FakeArgs())
    captured = capsys.readouterr()

    assert "[ai-router]" in captured.out
    assert "[Arix]" in captured.out
    assert "[ai-router] a.py:1-5 [f1]" in captured.out
    assert "[Arix] b.py:1-5 [f2]" in captured.out
    assert "\na.py:1-5 [f1]" not in captured.out
    assert not captured.out.startswith("a.py:1-5 [f1]")


def test_default_search_output_unchanged_without_all_repos(monkeypatch, capsys):
    class FakeArgs:
        query = "test"
        k = 5
        graph = False
        repo = ""
        # all_repos attribute intentionally omitted to test getattr fallback

    class FakeModel:
        def embed(self, texts, prefix=""):
            import numpy as np
            return np.array([[0.0] * 384])

    monkeypatch.setattr(ci, "get_model", FakeModel)

    rows = [
        (1, "path1.py", 1, 100, "func_1", "short chunk text"),
    ]

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            return None

        def fetchall(self):
            return rows

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(os, "environ", {"POSTGRES_DSN": "dummy"})

    ci.cmd_search(FakeArgs())
    captured = capsys.readouterr()

    assert captured.out.startswith("path1.py:1-100 [func_1]\n")
    assert not captured.out.startswith("[")


def test_ingest_rejects_flag_like_repo_name(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_project_info_for", lambda p: ("--bad-flag", None))
    with pytest.raises(ci.InvalidRepoIdentity):
        ci.ingest(force=True, repo_path=tmp_path)


def test_chunk_files_rejects_flag_like_path():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        ci.reject_flag_like("--looks-like-a-flag")
