import os
import subprocess
import sys
from pathlib import Path

import pytest

src_dir = str(Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, src_dir)

import id_alloc  # noqa: E402  (sys.path is set above so the src module resolves)

# T-915 phase B/C: cmd_next's max() no longer consults parse_registry() at all
# (it comes only from the locked ID-LEDGER.tsv now — see id_alloc.cmd_next).
# These tests used to drive their assertions through `id_alloc next`, which
# happened to also exercise parse_registry()'s prose/label/row-shape parsing
# because next's max used to be max(registry_max, ledger_max). That coupling
# is gone by design, so these now call parse_registry() directly: it is still
# the exact function `check` relies on for "manually assigned" reporting, and
# is still the thing standing between a prose mention and a false allocation.


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    yield tmp_path

def _registry_max(prefix):
    found = id_alloc.parse_registry()
    nums = [num for full_id, (p, num) in found.items() if p == prefix]
    return max(nums) if nums else 0

def test_parse_prose_is_ignored(isolated_paths):
    # Case (a): Real rows up to T-168, plus prose with T-0900
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- T-167 — something\n"
        "- T-168 — something else\n"
        "> **Overflow:** once a prefix reaches 900, the same prefix goes four digits (T-0900).\n",
        encoding="utf-8"
    )

    found = id_alloc.parse_registry()
    assert _registry_max("T") == 168
    assert "T-0900" not in found

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

    found = id_alloc.parse_registry()
    assert _registry_max("D") == 5
    assert _registry_max("N") == 3
    assert "D-9999" not in found
    assert "N-0900" not in found

def test_parse_clean_registry(isolated_paths):
    # Case (c): Clean registry with only real structural rows. Uses R (research
    # finding, T-014) rather than the retired W prefix (T-915 Phase A).
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- R-001 — one\n"
        "| R-002 | two |\n"
        "- R-003 — three\n",
        encoding="utf-8"
    )

    assert _registry_max("R") == 3

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

    found = id_alloc.parse_registry()
    # T-205 seen (label skipped), T-0900 still rejected (blockquote prose).
    assert _registry_max("T") == 205
    assert "T-0900" not in found


def test_labelled_row_body_mentions_are_not_allocations(isolated_paths):
    """Only the FIRST id on a row is the row's identifier. Ids quoted inside a
    row's body — typically VOIDed ones — must not be re-admitted."""
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "- T-300 — ⛔ VOID — duplicate of T-880. Original text mentioned T-901.\n",
        encoding="utf-8",
    )

    found = id_alloc.parse_registry()
    assert _registry_max("T") == 300
    assert "T-880" not in found
    assert "T-901" not in found

def test_prose_poisoning_does_not_move_next(isolated_paths):
    """End-to-end regression for the exact trap the registry's own header
    documents: mentioning a high, un-allocated id in prose (e.g. discussing the
    T-0900 four-digit overflow rule, or referencing a future/reserved id) must
    never move `next`'s output, because `next`'s max() no longer looks at the
    registry at all (T-915 phase B/C) — only the locked ledger counts."""
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text(
        "> **سرریز:** once a prefix reaches 900 it goes four-digit (`T-0900`).\n"
        "> See also the reserved id T-9999 for future overflow testing.\n",
        encoding="utf-8"
    )

    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    cmd = [sys.executable, "-m", "id_alloc", "next", "T", "--intent", "poison_check"]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    # Ledger is empty; a poisoned max would jump to T-9999+1 or similar.
    assert res.stdout.strip() == "T-001"
