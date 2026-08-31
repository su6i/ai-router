import os
import subprocess
import sys
from pathlib import Path

import pytest

src_dir = str(Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, src_dir)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    yield tmp_path

def test_parse_prose_is_ignored(isolated_paths):
    # Case (a): Real rows up to T-168, plus prose with T-0900
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- T-167 — something\n"
        "- T-168 — something else\n"
        "> **Overflow:** once a prefix reaches 900, the same prefix goes four digits (T-0900).\n",
        encoding="utf-8"
    )
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    cmd_next = [sys.executable, "-m", "id_alloc", "next", "T", "--intent", "test_ignore_prose"]
    res = subprocess.run(cmd_next, env=env, capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "T-169"

def test_parse_multiple_prefixes_prose_ignored(isolated_paths):
    # Case (b): Real rows for D and N, plus prose with D-9999 and N-0900
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- D-004 — four\n"
        "- D-005 — five\n"
        "| N-002 | ... |\n"
        "| N-003 | ... |\n"
        "Some text mentioning D-9999 and N-0900 mid-sentence.\n",
        encoding="utf-8"
    )
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    cmd_next_d = [sys.executable, "-m", "id_alloc", "next", "D", "--intent", "test_d"]
    res_d = subprocess.run(cmd_next_d, env=env, capture_output=True, text=True, check=True)
    assert res_d.stdout.strip() == "D-006"
    
    cmd_next_n = [sys.executable, "-m", "id_alloc", "next", "N", "--intent", "test_n"]
    res_n = subprocess.run(cmd_next_n, env=env, capture_output=True, text=True, check=True)
    assert res_n.stdout.strip() == "N-004"

def test_parse_clean_registry(isolated_paths):
    # Case (c): Clean registry with only real structural rows
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- W-001 — one\n"
        "| W-002 | two |\n"
        "- W-003 — three\n",
        encoding="utf-8"
    )
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    cmd_next_w = [sys.executable, "-m", "id_alloc", "next", "W", "--intent", "test_w"]
    res_w = subprocess.run(cmd_next_w, env=env, capture_output=True, text=True, check=True)
    assert res_w.stdout.strip() == "W-004"

def test_parse_labelled_row_is_an_allocation(isolated_paths):
    """A row may carry a work-order label before the id — 9 such rows hold real
    allocations in the live registry (e.g. `- WO-ARX-0072 / T-925 — ...`).
    Missing them is worse than the prose bug: an unseen maximum makes the
    allocator re-issue a live id, the collision class recorded for D-211."""
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- T-100 — a bare row\n"
        "- WO-ARX-0072 / T-205 — a labelled row, still a real allocation\n"
        "> **سرریز:** at 900 the prefix goes four-digit (`T-0900`).\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    cmd = [sys.executable, "-m", "id_alloc", "next", "T", "--intent", "labelled"]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    # T-205 seen (label skipped), T-0900 still rejected (blockquote prose).
    assert res.stdout.strip() == "T-206"


def test_labelled_row_body_mentions_are_not_allocations(isolated_paths):
    """Only the FIRST id on a row is the row's identifier. Ids quoted inside a
    row's body — typically VOIDed ones — must not be re-admitted."""
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- T-300 — ⛔ VOID — duplicate of T-880. Original text mentioned T-901.\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    cmd = [sys.executable, "-m", "id_alloc", "next", "T", "--intent", "body"]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "T-301"
