import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d
import skills_index


def _pg_available() -> bool:
    try:
        import psycopg

        d.load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False
        psycopg.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False


has_pg = _pg_available()


def test_missing_skills_dir_returns_zero_stats(tmp_path):
    stats = skills_index.ingest(root=tmp_path)
    assert stats["files_seen"] == 0
    assert stats["chunks_written"] == 0
    assert stats["chunks_deleted"] == 0
    assert stats["skipped"] == 0
    assert stats["total_chunks"] == 0
    assert stats["total_docs"] == 0


@pytest.mark.skipif(not has_pg, reason="Missing Postgres")
def test_skills_ingest_and_skip(tmp_path):
    # `file_path` is stored relative to the ingest root, so fixture files named
    # like real skills would share a key with them. Unique names keep this test
    # in its own corner of the shared table, and prune=False stops a two-file
    # fixture run from deleting the real index as "no longer present".
    skills_dir = tmp_path / ".agent" / "constitution" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:12]

    (skills_dir / f"zz-test-{tag}-one.md").write_text(
        f"# Skill One {tag}\nThis is the first skill definition."
    )
    (skills_dir / f"zz-test-{tag}-two.md").write_text(
        f"# Skill Two {tag}\nThis is the second skill definition."
    )

    stats1 = skills_index.ingest(force=False, root=tmp_path, prune=False)
    assert stats1["files_seen"] == 2
    assert stats1["chunks_written"] >= 2

    stats2 = skills_index.ingest(force=False, root=tmp_path, prune=False)
    assert stats2["files_seen"] == 0
    assert stats2["skipped"] == 2


@pytest.mark.skipif(not has_pg, reason="Missing Postgres")
def test_single_target_file_ingest_does_not_prune_the_rest(tmp_path):
    """A single-file ingest must not delete every other indexed skill."""
    skills_dir = tmp_path / ".agent" / "constitution" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:12]

    a = skills_dir / f"zz-target-{tag}-a.md"
    b = skills_dir / f"zz-target-{tag}-b.md"
    a.write_text(f"# A {tag}\nfirst")
    b.write_text(f"# B {tag}\nsecond")

    skills_index.ingest(force=True, root=tmp_path, prune=False)
    before = skills_index.ingest(force=False, root=tmp_path, prune=False)["total_docs"]

    # Re-ingesting only one file must leave the other one indexed.
    skills_index.ingest(force=True, root=tmp_path, target_file=a)
    after = skills_index.ingest(force=False, root=tmp_path, prune=False)["total_docs"]
    assert after == before
