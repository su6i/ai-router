import io
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hooks.session_start_brief as hook

def test_postgres_unreachable_falls_back(monkeypatch, capsys):
    import psycopg
    
    monkeypatch.setattr(hook, "_model_is_cached", lambda: True)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/db")
    monkeypatch.setattr(hook, "load_env", lambda: None)
    
    def mock_search(*args):
        raise psycopg.OperationalError("mock")
        
    monkeypatch.setattr(hook, "_search_session_chunks", mock_search)
    monkeypatch.setattr(hook, "repo_slug", lambda cwd: "test-repo")
    
    ctx = hook.build_additional_context("/tmp")
    assert "SESSION.md" in ctx
    
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"cwd":"/tmp"}'))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"

def test_ping_only_chunks_dropped():
    rows = [
        ("Heading 1", "- last: abc123 feat: x\n- session: xyz\n· branch feat/x ·\nbehind:-\ndirty:0\n- session: foo\n- session: bar\n", "2023-01-01"),
        ("Heading 2", "Real chunk 1 text that has some substance.", "2023-01-02"),
        ("Heading 3", "· branch feat/y ·\n- last: qwe", "2023-01-03"),
        ("Heading 4", "Real chunk 2 is also here with more words.", "2023-01-04"),
        ("Heading 5", "- session: something\n- last: x\n· branch feat/x ·\nbehind:-\ndirty:0\n- session: a\n- session: b\n", "2023-01-05")
    ]
    
    res = hook._rank_and_format(rows)
    assert "dirty:0" not in res
    assert "behind:-" not in res
    assert "Real chunk 1 text" in res
    assert "Real chunk 2 is also here" in res

def test_boost_works():
    rows = [
        ("Ping", "- last: a\n- last: b\n- last: c\nbehind:-\ndirty:0\n", "2023-01-01"),
        ("Old But Boosted", "We Left Open a few questions about the network.", "2023-01-01"),
        ("Newer Prosey", "Just some normal text here that does not contain boost terms.", "2023-01-02")
    ]
    res = hook._rank_and_format(rows)
    assert "Left Open" in res

def test_todo_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "_agent_projects_root", lambda: tmp_path)
    mem_dir = tmp_path / "_memory"
    mem_dir.mkdir(parents=True)
    todo_file = mem_dir / "TODO.md"
    
    todo_content = """# Global TODO
## ai-router
- [ ] Needs doing
- [~] In progress thing
- [x] Done thing
- [⛔] Blocked thing

## other
- [ ] Other project thing
"""
    todo_file.write_text(todo_content)
    
    res = hook._todo_open_items_block("ai-router")
    assert "Needs doing" in res
    assert "In progress thing" in res
    assert "Done thing" not in res
    assert "Blocked thing" not in res
    assert "Other project thing" not in res

def test_cap_enforcement(monkeypatch):
    long_str = "x" * 2000
    
    monkeypatch.setattr(hook, "_todo_open_items_block", lambda slug: long_str)
    monkeypatch.setattr(hook, "_get_continuity_block", lambda slug: long_str)
    monkeypatch.setattr(hook, "_inbox_block", lambda slug: long_str)
    monkeypatch.setattr(hook, "_pointer_tail", lambda slug: long_str)
    monkeypatch.setattr(hook, "repo_slug", lambda cwd: "test-repo")
    
    res = hook.build_additional_context("/tmp")
    assert len(res) <= hook.TOTAL_CAP

def test_timeout_enforcement(monkeypatch):
    monkeypatch.setattr(hook, "RAG_TIMEOUT_S", 0.2)
    monkeypatch.setattr(hook, "_model_is_cached", lambda: True)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake/db")
    monkeypatch.setattr(hook, "load_env", lambda: None)
    
    def mock_search(*args):
        time.sleep(1.0)
        return []
        
    monkeypatch.setattr(hook, "_search_session_chunks", mock_search)
    
    res = hook._get_continuity_block("test-repo")
    assert "SESSION.md" in res


def test_stale_chunks_are_dropped():
    """A digest that still says 'Left Open' about work shipped two months ago
    makes a session re-raise closed items. Recency is measured against the
    freshest chunk retrieved, never an absolute date."""
    rows = [
        ("Fresh", "Left Open: the live item that still matters.", "2026-09-01"),
        ("Stale", "Left open: something that shipped in July.", "2026-07-11"),
        ("Older", "Left Open: something that shipped even earlier.", "2026-07-07"),
    ]
    res = hook._rank_and_format(rows)
    assert "still matters" in res
    assert "shipped in July" not in res
    assert "even earlier" not in res


def test_stale_guard_keeps_everything_when_all_are_old():
    """If nothing is recent, an old brief still beats an empty one."""
    rows = [
        ("A", "Left Open: alpha item.", "2026-07-11"),
        ("B", "Left Open: beta item.", "2026-07-07"),
    ]
    res = hook._rank_and_format(rows)
    assert "alpha item" in res and "beta item" in res


def test_zero_boost_chunks_dropped_when_boosted_exist():
    """A nearest-neighbour chunk that states no open state is topical noise;
    it must not spend the cap when real continuity chunks survived."""
    rows = [
        ("A", "Left Open: alpha.", "2026-09-01"),
        ("B", "Next Work Order: beta.", "2026-09-01"),
        ("C", "def render_tasks() -> str: reference documentation.", "2026-09-01"),
    ]
    res = hook._rank_and_format(rows)
    assert "alpha" in res and "beta" in res
    assert "render_tasks" not in res


def test_heading_not_printed_twice():
    """Chunks usually begin with their own heading; printing it again spends
    the cap duplicating every heading."""
    rows = [
        ("## Session digest — 2026-09-01",
         "## Session digest — 2026-09-01\nLeft Open: alpha.", "2026-09-01"),
        ("## Session digest — 2026-08-30",
         "## Session digest — 2026-08-30\nLeft Open: beta.", "2026-08-30"),
    ]
    res = hook._rank_and_format(rows)
    assert res.count("## Session digest — 2026-09-01") == 1


def test_todo_multiline_bullets_kept(monkeypatch, tmp_path):
    """TODO bullets wrap; keeping only the first line truncates most items
    mid-sentence and hides the branch name that makes them actionable."""
    memory = tmp_path / "_memory"
    memory.mkdir(parents=True)
    (memory / "TODO.md").write_text(
        "## demo\n"
        "- [~] **wo-0001** — a wrapped item\n"
        "  continues here with `feat/some-branch`.\n"
        "- [x] **wo-0002** — done, must not appear\n"
        "  closed continuation must not appear either.\n"
        "## other\n"
        "- [ ] belongs to another project\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook, "_agent_projects_root", lambda: tmp_path)

    block = hook._todo_open_items_block("demo")
    assert "feat/some-branch" in block
    assert "must not appear" not in block
    assert "another project" not in block
