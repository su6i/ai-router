"""Tests for code_index divergence scenarios.

Covers the cases where the on-disk state of a repo diverges from what the
index holds: stale commits, vanished files, cross-repo ingested_files key
collisions, and the chunk_generic fallback for top-level scripts with no
def/class nodes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import code_index as ci  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import has_pg  # noqa: E402

requires_pg = pytest.mark.skipif(not has_pg, reason="Missing Postgres")


# ---------------------------------------------------------------------------
# Unit tests (no Postgres)
# ---------------------------------------------------------------------------


class TestIngstedKey:
    """_ingested_key must prefix every key with '<repo>::' to prevent
    cross-repo hash-row collisions in the shared ingested_files table."""

    def test_prefix_present(self):
        key = ci._ingested_key("my-repo", "src/__init__.py")
        assert key == "my-repo::src/__init__.py"

    def test_different_repos_different_keys(self):
        key_a = ci._ingested_key("repo-a", "install.sh")
        key_b = ci._ingested_key("repo-b", "install.sh")
        assert key_a != key_b

    def test_same_repo_same_key(self):
        assert ci._ingested_key("r", "p") == ci._ingested_key("r", "p")

    def test_separator_not_present_in_plain_path(self):
        # A bare path without the prefix must NOT equal the prefixed key.
        bare = "src/main.py"
        assert ci._ingested_key("repo", bare) != bare


class TestChunkGeneric:
    """chunk_generic must split large files at ~400-token line boundaries and
    handle edge cases: empty input, single-line files, and files that fit in
    one block."""

    def test_empty_bytes_returns_empty(self):
        assert ci.chunk_generic(b"") == []

    def test_whitespace_only_content_produces_chunk(self):
        # chunk_generic does not strip internally; callers (cmd_chunk_files)
        # filter with `if c['text'].strip()`. Whitespace bytes still produce
        # a chunk — the stripping responsibility lives one level up.
        chunks = ci.chunk_generic(b"   \n\n  ")
        assert len(chunks) == 1
        assert chunks[0]["text"].strip() == ""

    def test_single_line(self):
        src = b"x = 1\n"
        chunks = ci.chunk_generic(src)
        assert len(chunks) == 1
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 1
        assert "x = 1" in chunks[0]["text"]

    def test_symbol_and_parent_are_none(self):
        chunks = ci.chunk_generic(b"a = 1\nb = 2\n")
        for c in chunks:
            assert c["symbol"] is None
            assert c["parent_symbol"] is None

    def test_large_file_splits_into_multiple_chunks(self):
        # Each line is ~6 chars; we need enough to exceed 400 tokens (chars//3).
        # 400 * 3 = 1200 chars -> need >200 six-char lines.
        lines = "\n".join(f"x_{i:04d}" for i in range(300))
        chunks = ci.chunk_generic(lines.encode())
        assert len(chunks) > 1

    def test_line_ranges_are_contiguous(self):
        lines = "\n".join(f"line_{i}" for i in range(50))
        chunks = ci.chunk_generic(lines.encode())
        if len(chunks) > 1:
            for prev, nxt in zip(chunks, chunks[1:]):
                assert nxt["start_line"] == prev["end_line"] + 1

    def test_last_chunk_end_line_matches_total_lines(self):
        lines = "\n".join(f"L{i}" for i in range(10))
        chunks = ci.chunk_generic(lines.encode())
        assert chunks[-1]["end_line"] == 10


class TestIsExcluded:
    """_is_excluded must reject vendored/generated paths even if git-tracked."""

    @pytest.mark.parametrize("rel", [
        "node_modules/lodash/index.js",
        ".venv/lib/python3.12/site-packages/foo.py",
        "venv/bin/activate",
        "dist/bundle.js",
        "build/output.js",
        "__pycache__/foo.cpython-312.pyc",
        ".git/COMMIT_EDITMSG",
        "static/vendor/jquery.min.js",
    ])
    def test_excluded_paths(self, rel):
        assert ci._is_excluded(rel) is True

    @pytest.mark.parametrize("rel", [
        "src/main.py",
        "install.sh",
        "lib/utils.js",
        "components/App.tsx",
    ])
    def test_included_paths(self, rel):
        assert ci._is_excluded(rel) is False


class TestLangForPath:
    """_lang_for_path must return the CODE_EXT_LANG tag or None."""

    @pytest.mark.parametrize("path,expected", [
        ("foo.py", "python"),
        ("foo.sh", "bash"),
        ("foo.js", "javascript"),
        ("foo.jsx", "javascript"),
        ("foo.ts", "typescript"),
        ("foo.tsx", "tsx"),
        ("foo.go", "go"),
        ("foo.rs", "rust"),
    ])
    def test_known_extensions(self, path, expected):
        assert ci._lang_for_path(path) == expected

    @pytest.mark.parametrize("path", ["README.md", "data.json", "img.png", ""])
    def test_unknown_extensions_return_none(self, path):
        assert ci._lang_for_path(path) is None


class TestCmdChunkFiles:
    """cmd_chunk_files must handle top-level scripts (no def/class) by falling
    back to chunk_generic instead of returning 0 chunks."""

    def test_top_level_script_fallback(self, tmp_path):
        """A .py file with only top-level statements (no def/class) must
        produce ≥1 chunk via the chunk_generic fallback."""
        script = tmp_path / "script.py"
        script.write_text(
            textwrap.dedent("""\
                import os
                import sys

                x = 1
                y = 2
                print(x + y)
            """),
            encoding="utf-8",
        )
        result = {}

        def fake_print(s):
            result.update(json.loads(s))

        with patch("builtins.print", side_effect=fake_print):
            ci.cmd_chunk_files([str(script)])

        key = str(script)
        assert key in result
        assert len(result[key]) >= 1

    def test_empty_file_yields_no_chunks(self, tmp_path):
        empty = tmp_path / "empty.py"
        empty.write_bytes(b"")
        result = {}

        with patch("builtins.print", side_effect=lambda s: result.update(json.loads(s))):
            ci.cmd_chunk_files([str(empty)])

        assert result.get(str(empty), []) == []

    def test_js_file_uses_generic_chunker(self, tmp_path):
        js_file = tmp_path / "app.js"
        js_file.write_text("const x = 1;\nconsole.log(x);\n", encoding="utf-8")
        result = {}

        with patch("builtins.print", side_effect=lambda s: result.update(json.loads(s))):
            ci.cmd_chunk_files([str(js_file)])

        key = str(js_file)
        assert key in result
        assert len(result[key]) >= 1
        # Generic chunker never sets a symbol
        assert all(c["symbol"] is None for c in result[key])


class TestGetRepoRoots:
    """get_repo_roots must read from config when present, fall back to the
    default scan, and silently drop non-existent paths."""

    def test_reads_dict_format(self, tmp_path, monkeypatch):
        cfg = tmp_path / "code_repo_roots.json"
        real_dir = tmp_path / "a-repo"
        real_dir.mkdir()
        cfg.write_text(json.dumps({"roots": [str(real_dir)]}))
        monkeypatch.setattr(ci.delegate, "DATA_DIR", tmp_path)
        roots = ci.get_repo_roots()
        assert real_dir in roots

    def test_reads_bare_list_format(self, tmp_path, monkeypatch):
        cfg = tmp_path / "code_repo_roots.json"
        real_dir = tmp_path / "b-repo"
        real_dir.mkdir()
        cfg.write_text(json.dumps([str(real_dir)]))
        monkeypatch.setattr(ci.delegate, "DATA_DIR", tmp_path)
        roots = ci.get_repo_roots()
        assert real_dir in roots

    def test_nonexistent_paths_silently_dropped(self, tmp_path, monkeypatch):
        cfg = tmp_path / "code_repo_roots.json"
        cfg.write_text(json.dumps({"roots": [str(tmp_path / "ghost")]}))
        monkeypatch.setattr(ci.delegate, "DATA_DIR", tmp_path)
        roots = ci.get_repo_roots()
        assert roots == []

    def test_missing_config_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ci.delegate, "DATA_DIR", tmp_path)
        monkeypatch.setattr(ci, "_default_repo_roots", lambda: [tmp_path])
        roots = ci.get_repo_roots()
        assert tmp_path in roots

    def test_bad_json_falls_back_to_default(self, tmp_path, monkeypatch):
        cfg = tmp_path / "code_repo_roots.json"
        cfg.write_text("{not valid json")
        monkeypatch.setattr(ci.delegate, "DATA_DIR", tmp_path)
        monkeypatch.setattr(ci, "_default_repo_roots", lambda: [tmp_path])
        roots = ci.get_repo_roots()
        assert tmp_path in roots


class TestProjectInfoFor:
    """_project_info_for must use the directory basename, not the git remote."""

    def test_returns_basename(self, tmp_path):
        repo_dir = tmp_path / "MyProject"
        repo_dir.mkdir()
        # Init a real git repo so git rev-parse works
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, capture_output=True)
        (repo_dir / "x.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, capture_output=True, check=True)

        name, commit = ci._project_info_for(repo_dir)
        assert name == "MyProject"
        assert commit and len(commit) == 7

    def test_no_git_returns_basename_and_none(self, tmp_path):
        repo_dir = tmp_path / "BareDir"
        repo_dir.mkdir()
        name, commit = ci._project_info_for(repo_dir)
        assert name == "BareDir"
        # The inner git() helper returns "" on failure; _project_info_for
        # uses `git(...) or None`, so a non-git directory yields None.
        assert commit is None


class TestExtractPythonCalls:
    """extract_python_calls must find caller→callee pairs from Python source."""

    def test_finds_top_level_call(self):
        src = "def foo():\n    bar()\n"
        calls = ci.extract_python_calls(src)
        assert ("foo", "bar") in calls

    def test_finds_method_call_in_class(self):
        src = textwrap.dedent("""\
            class C:
                def method(self):
                    helper()
        """)
        calls = ci.extract_python_calls(src)
        assert ("C.method", "helper") in calls

    def test_empty_source_returns_empty(self):
        assert ci.extract_python_calls("") == set()

    def test_syntax_error_returns_empty(self):
        assert ci.extract_python_calls("def (broken:") == set()

    def test_attribute_call_captured(self):
        src = "def f():\n    obj.do_thing()\n"
        calls = ci.extract_python_calls(src)
        assert ("f", "do_thing") in calls


# ---------------------------------------------------------------------------
# Integration tests (Postgres required)
# ---------------------------------------------------------------------------


@requires_pg
def test_ingest_creates_and_gc_chunks(tmp_path, monkeypatch):
    """ingest() must write chunks for a new file and GC them when the file
    is deleted, using a fresh isolated git repo so the test is idempotent."""
    import psycopg

    dsn = os.environ["POSTGRES_DSN"]

    # Build a minimal git repo with one Python file
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)

    src_file = repo / "mod.py"
    src_file.write_text(
        textwrap.dedent("""\
            def alpha():
                return 1

            def beta():
                return alpha()
        """),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

    repo_name = repo.name

    # Patch project_info so ingest() picks up our repo name
    monkeypatch.setattr(ci, "project_info", lambda: (repo_name, "unknown"))

    stats = ci.ingest(force=True, repo_path=repo)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM code_chunks WHERE repo = %s", (repo_name,)
            )
            count_after_ingest = cur.fetchone()[0]

    assert count_after_ingest >= 1, "Expected ≥1 chunk after first ingest"
    assert stats["chunks_written"] >= 1

    # Now delete the file, commit, and force-reingest: GC must remove old chunks
    src_file.unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "rm mod.py"], cwd=repo, capture_output=True, check=True)

    ci.ingest(force=True, repo_path=repo)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM code_chunks WHERE repo = %s AND path = %s",
                (repo_name, "mod.py"),
            )
            count_after_gc = cur.fetchone()[0]
            # Cleanup
            cur.execute("DELETE FROM code_chunks WHERE repo = %s", (repo_name,))
            cur.execute(
                "DELETE FROM ingested_files WHERE collection = 'code' AND file_path LIKE %s",
                (f"{repo_name}::%",),
            )
        conn.commit()

    assert count_after_gc == 0, "GC must remove chunks for deleted files"


@requires_pg
def test_ingested_key_prefix_prevents_cross_repo_collision(tmp_path, monkeypatch):
    """Two repos sharing the same relative path must have independent
    ingested_files rows (keyed by their respective prefixes)."""
    import psycopg

    dsn = os.environ["POSTGRES_DSN"]

    def _make_repo(base: Path, name: str) -> Path:
        repo = base / name
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (repo / "install.sh").write_text(f"#!/bin/sh\necho {name}\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
        return repo

    repo_a = _make_repo(tmp_path, "proj-a-divergence")
    repo_b = _make_repo(tmp_path, "proj-b-divergence")

    ci.ingest(force=True, repo_path=repo_a)
    ci.ingest(force=True, repo_path=repo_b)

    key_a = ci._ingested_key("proj-a-divergence", "install.sh")
    key_b = ci._ingested_key("proj-b-divergence", "install.sh")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_path FROM ingested_files WHERE collection = 'code' AND file_path IN (%s, %s)",
                (key_a, key_b),
            )
            found = {r[0] for r in cur.fetchall()}
            # Cleanup
            cur.execute(
                "DELETE FROM ingested_files WHERE collection = 'code' AND file_path IN (%s, %s)",
                (key_a, key_b),
            )
            for name in ("proj-a-divergence", "proj-b-divergence"):
                cur.execute("DELETE FROM code_chunks WHERE repo = %s", (name,))
        conn.commit()

    assert key_a in found, "proj-a-divergence key must be in ingested_files"
    assert key_b in found, "proj-b-divergence key must be in ingested_files"


@requires_pg
def test_ingest_idempotent_second_run_writes_zero_chunks(tmp_path, monkeypatch):
    """A second force=True ingest on an unchanged repo must write 0 new chunks
    (all hashes match → upsert path, not insert)."""
    import psycopg

    dsn = os.environ["POSTGRES_DSN"]

    repo = tmp_path / "idempotent-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    (repo / "main.py").write_text("def run():\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

    repo_name = repo.name

    stats1 = ci.ingest(force=True, repo_path=repo)
    stats2 = ci.ingest(force=True, repo_path=repo)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM code_chunks WHERE repo = %s", (repo_name,))
            cur.execute(
                "DELETE FROM ingested_files WHERE collection = 'code' AND file_path LIKE %s",
                (f"{repo_name}::%",),
            )
        conn.commit()

    assert stats1["chunks_written"] >= 1, "First ingest must write ≥1 chunk"
    assert stats2["chunks_written"] == 0, "Second ingest must write 0 new chunks (idempotent)"


# ---------------------------------------------------------------------------
# T-970: sweep lock, divergence self-heal, --status wiring
# ---------------------------------------------------------------------------


class TestSweepLock:
    """_SweepLock must be a real mutual-exclusion primitive: a second
    acquire while the first is held must fail, and release must free it
    for the next acquirer."""

    def test_second_acquire_fails_while_held(self):
        lock1 = ci._SweepLock()
        assert lock1.acquire() is True
        lock2 = ci._SweepLock()
        assert lock2.acquire() is False
        lock1.release()
        assert lock2.acquire() is True
        lock2.release()

    def test_sweep_returns_locked_out_when_lock_held(self, monkeypatch):
        """sweep() must do zero work and report locked_out=True instead of
        blocking or corrupting anything when another holder already has
        the lock."""
        held = ci._SweepLock()
        assert held.acquire() is True
        try:
            monkeypatch.setattr(ci, "get_repo_roots", lambda: [])
            result = ci.sweep()
        finally:
            held.release()
        assert result.get("locked_out") is True
        assert result["repos_seen"] == 0


def _make_git_repo(base: Path, name: str, filename: str = "mod.py") -> Path:
    repo = base / name
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    (repo / filename).write_text(
        textwrap.dedent("""\
            def alpha():
                return 1
        """),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


@requires_pg
class TestDivergenceSelfHeal:
    """The T-970 core scenario: a repo's code_chunks rows vanish (e.g. a
    concurrent run's GC) while its ingested_files rows survive -- the next
    sweep must detect it, heal it, leave other repos untouched, and still
    exit cleanly."""

    def test_diverged_repo_detected_and_healed_other_untouched(self, tmp_path, monkeypatch):
        import psycopg

        dsn = os.environ["POSTGRES_DSN"]
        repo_a = _make_git_repo(tmp_path, "divergence-heal-a")
        repo_b = _make_git_repo(tmp_path, "divergence-heal-b")

        ci.ingest(force=True, repo_path=repo_a)
        ci.ingest(force=True, repo_path=repo_b)

        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM code_chunks WHERE repo = %s", ("divergence-heal-a",))
                    assert cur.fetchone()[0] > 0
                    cur.execute("SELECT count(*) FROM code_chunks WHERE repo = %s", ("divergence-heal-b",))
                    b_chunks_before = cur.fetchone()[0]
                    assert b_chunks_before > 0

                    # Reproduce the real T-970 failure directly: wipe
                    # repo_a's code_chunks rows but leave its
                    # ingested_files rows in place (the divergence).
                    cur.execute("DELETE FROM code_chunks WHERE repo = %s", ("divergence-heal-a",))
                conn.commit()

            monkeypatch.setattr(ci, "get_repo_roots", lambda: [repo_a, repo_b])

            result = ci.sweep()

            assert result["repos_failed"] == 0
            assert "divergence-heal-a" in result["empty_repos_healed"]
            assert result["empty_repos_unhealed"] == []

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM code_chunks WHERE repo = %s", ("divergence-heal-a",))
                    a_chunks_after = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM code_chunks WHERE repo = %s", ("divergence-heal-b",))
                    b_chunks_after = cur.fetchone()[0]

            assert a_chunks_after > 0, "diverged repo must be healed (chunks rewritten)"
            assert b_chunks_after == b_chunks_before, "healing one repo must never touch another's rows"
        finally:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    for name in ("divergence-heal-a", "divergence-heal-b"):
                        cur.execute("DELETE FROM code_chunks WHERE repo = %s", (name,))
                        cur.execute(
                            "DELETE FROM ingested_files WHERE collection = 'code' AND file_path LIKE %s",
                            (f"{name}::%",),
                        )
                conn.commit()

    def test_second_sweep_after_heal_is_clean(self, tmp_path, monkeypatch):
        """A second sweep run right after the heal must find nothing left
        to heal -- the whole point of DoD 1/2 is that the NEXT sweep finds
        the repo clean."""
        import psycopg

        dsn = os.environ["POSTGRES_DSN"]
        repo = _make_git_repo(tmp_path, "divergence-heal-clean")
        ci.ingest(force=True, repo_path=repo)

        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM code_chunks WHERE repo = %s", ("divergence-heal-clean",))
                conn.commit()

            monkeypatch.setattr(ci, "get_repo_roots", lambda: [repo])
            first = ci.sweep()
            assert "divergence-heal-clean" in first["empty_repos_healed"]

            second = ci.sweep()
            assert second["empty_repos"] == []
            assert second["empty_repos_healed"] == []
            assert second["empty_repos_unhealed"] == []
        finally:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM code_chunks WHERE repo = %s", ("divergence-heal-clean",))
                    cur.execute(
                        "DELETE FROM ingested_files WHERE collection = 'code' AND file_path LIKE %s",
                        ("divergence-heal-clean::%",),
                    )
                conn.commit()


@requires_pg
class TestRepoStatusDivergedFlag:
    """repo_status() must flag a repo with registered files and zero
    chunks as diverged=True (DoD 5)."""

    def test_diverged_repo_flagged(self, tmp_path):
        import psycopg

        dsn = os.environ["POSTGRES_DSN"]
        repo = _make_git_repo(tmp_path, "status-diverged-repo", filename="m.py")
        repo_name = repo.name

        try:
            ci.ingest(force=True, repo_path=repo)
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM code_chunks WHERE repo = %s", (repo_name,))
                conn.commit()

            status = ci.repo_status()
            assert repo_name in status
            assert status[repo_name]["diverged"] is True
            assert status[repo_name]["chunks"] == 0
            assert status[repo_name]["files"] > 0
        finally:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM code_chunks WHERE repo = %s", (repo_name,))
                    cur.execute(
                        "DELETE FROM ingested_files WHERE collection = 'code' AND file_path LIKE %s",
                        (f"{repo_name}::%",),
                    )
                conn.commit()


@requires_pg
class TestVanishedFileDeleteSiteClearsIngestedFiles:
    """The `vanished` files DELETE path (incremental ingest of a repo whose
    tracked file was removed) must clear the matching ingested_files key,
    not just the code_chunks row -- DoD 3's guarantee, exercised for the one
    DELETE site not already covered by test_ingest_creates_and_gc_chunks
    (which only exercises the force=True whole-rebuild GC path)."""

    def test_vanished_file_clears_ingested_files_row(self, tmp_path):
        import psycopg

        dsn = os.environ["POSTGRES_DSN"]
        repo = tmp_path / "vanished-delete-site-repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        f = repo / "gone.py"
        f.write_text("def g():\n    return 1\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

        repo_name = repo.name

        try:
            ci.ingest(force=True, repo_path=repo)
            key = ci._ingested_key(repo_name, "gone.py")

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM ingested_files WHERE collection = 'code' AND file_path = %s",
                        (key,),
                    )
                    assert cur.fetchone()[0] == 1

            f.unlink()
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "remove gone.py"], cwd=repo, capture_output=True, check=True)

            # Incremental (force=False) ingest so the "vanished" branch
            # (git diff against the last indexed commit) runs, not the
            # separate force=True whole-rebuild GC path.
            ci.ingest(force=False, repo_path=repo)

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM ingested_files WHERE collection = 'code' AND file_path = %s",
                        (key,),
                    )
                    remaining = cur.fetchone()[0]
                    cur.execute(
                        "SELECT count(*) FROM code_chunks WHERE repo = %s AND path = %s",
                        (repo_name, "gone.py"),
                    )
                    chunk_remaining = cur.fetchone()[0]

            assert remaining == 0, "vanished-file DELETE site must also clear the ingested_files row"
            assert chunk_remaining == 0
        finally:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM code_chunks WHERE repo = %s", (repo_name,))
                    cur.execute(
                        "DELETE FROM ingested_files WHERE collection = 'code' AND file_path LIKE %s",
                        (f"{repo_name}::%",),
                    )
                conn.commit()
