import os
import subprocess
import sys
from pathlib import Path

import pytest

src_dir = str(Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, src_dir)

import id_alloc  # noqa: E402  (sys.path is set above so the src module resolves)

@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    yield tmp_path

def test_concurrency(isolated_paths):
    # Test case 1: spawn 8 concurrent processes to request next D
    num_processes = 8
    tmp_path_str = str(isolated_paths)
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = tmp_path_str
    env["PYTHONPATH"] = src_dir
    
    procs = []
    for i in range(num_processes):
        cmd = [sys.executable, "-m", "id_alloc", "next", "D", "--intent", f"worker_{i}"]
        p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, text=True)
        procs.append(p)
        
    results = []
    for p in procs:
        p.wait()
        results.append(p.stdout.read().strip())
        
    unique_ids = set(results)
    assert len(unique_ids) == num_processes, f"Expected {num_processes} unique IDs, got {len(unique_ids)}: {results}"
    for i in range(1, num_processes + 1):
        assert f"D-{i:03d}" in unique_ids

def test_registry_offset_and_check(isolated_paths):
    # T-915 phase B/C: next's max() comes only from the locked ledger, never
    # from the registry (that coupling was the D-173/D-174/N-035 root cause).
    # Registry has D-200, ledger empty -> next D is D-001, NOT D-201: a manual
    # registry entry that never made it into the ledger no longer moves max.
    # check still uses parse_registry() for reporting, so it still flags
    # D-200 as manually assigned -- that's the signal an operator (or the
    # SessionStart hook) uses to run `seed` and close the gap before it can
    # ever collide.
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text("- D-200 — manually added\n", encoding="utf-8")

    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    # 2. next D ignores the registry-only entry and returns D-001
    cmd_next = [sys.executable, "-m", "id_alloc", "next", "D", "--intent", "test_offset"]
    res_next = subprocess.run(cmd_next, env=env, capture_output=True, text=True, check=True)
    assert res_next.stdout.strip() == "D-001"

    # 3. check should exit 1 and report D-200 as manually assigned
    cmd_check = [sys.executable, "-m", "id_alloc", "check"]
    res_check = subprocess.run(cmd_check, env=env, capture_output=True, text=True)
    assert res_check.returncode == 1
    assert "manually assigned: D-200" in res_check.stdout

def test_seed_deduplication(isolated_paths):
    # Test case 4: fake registry with 3 occurrences of T-162 -> seed -> exactly 1 line
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text("- T-162 — first\n- T-162 — second\n- T-162 — third\n", encoding="utf-8")
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    cmd_seed = [sys.executable, "-m", "id_alloc", "seed"]
    subprocess.run(cmd_seed, env=env, check=True)
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("T-162\t")

def test_invalid_prefix(isolated_paths):
    # Test case 5: unknown/disallowed prefix -> exit code 2
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    cmd_invalid = [sys.executable, "-m", "id_alloc", "next", "BOGUS", "--intent", "x"]
    res = subprocess.run(cmd_invalid, env=env, capture_output=True, text=True)
    assert res.returncode == 2

def test_registry_only_ids_are_not_also_reported_as_gaps(isolated_paths, capsys):
    # A number that lives in the registry is taken, not free. Counting it as a gap
    # once made a fresh ledger print one "gap" line per historical ID, drowning the
    # two findings that actually mean something.
    (isolated_paths / "REGISTRY-IDS.md").write_text(
        "- D-001 — one\n- D-002 — two\n- D-004 — four\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit) as exc:
        id_alloc.cmd_check(None)
    out = capsys.readouterr().out
    assert exc.value.code == 1  # three manually assigned IDs
    assert out.count("gap in sequence") == 1
    assert "gap in sequence: D-003" in out
    for taken in ("gap in sequence: D-001", "gap in sequence: D-002", "gap in sequence: D-004"):
        assert taken not in out

def test_seed_idempotent(isolated_paths):
    # Idempotency guarantee: seed run twice adds no new lines on second run
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text("- T-162 — first\n", encoding="utf-8")
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    subprocess.run([sys.executable, "-m", "id_alloc", "seed"], env=env, check=True)
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    bytes_first = ledger_path.read_bytes()
    
    subprocess.run([sys.executable, "-m", "id_alloc", "seed"], env=env, check=True)
    bytes_second = ledger_path.read_bytes()
    
    assert bytes_first == bytes_second

def test_void_id_is_not_reissued(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text("D-173\t2026-09-03T00:00:00Z\ttest\tfirst issue\n", encoding="utf-8")
    
    subprocess.run([sys.executable, "-m", "id_alloc", "void", "D-173", "--reason", "duplicate test"], env=env, check=True)
    
    res = subprocess.run([sys.executable, "-m", "id_alloc", "next", "D", "--intent", "next test"], env=env, capture_output=True, text=True, check=True)
    
    assert res.stdout.strip() == "D-174"

def test_void_unknown_id_is_refused(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text("D-173\t2026-09-03T00:00:00Z\ttest\tfirst issue\n", encoding="utf-8")

    res = subprocess.run([sys.executable, "-m", "id_alloc", "void", "D-999", "--reason", "typo"],
                         env=env, capture_output=True, text=True)
    assert res.returncode != 0
    # the ledger must be untouched: a refused void may not move `max`
    assert ledger_path.read_text(encoding="utf-8").count("D-999") == 0
    nxt = subprocess.run([sys.executable, "-m", "id_alloc", "next", "D", "--intent", "after refused void"],
                         env=env, capture_output=True, text=True, check=True)
    assert nxt.stdout.strip() == "D-174"

def test_check_clean_ledger(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text("D-001\t2026-09-03T00:00:00Z\ttest\ttest intent\n", encoding="utf-8")
    
    res = subprocess.run([sys.executable, "-m", "id_alloc", "check"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "duplicated" in res.stdout
    assert "0 duplicated" in res.stdout

def test_check_genuine_duplicate(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text(
        "D-001\t2026-09-03T00:00:00Z\ttest\ttest intent 1\n"
        "D-001\t2026-09-03T00:01:00Z\ttest\ttest intent 2\n",
        encoding="utf-8"
    )
    
    res = subprocess.run([sys.executable, "-m", "id_alloc", "check"], env=env, capture_output=True, text=True)
    assert res.returncode == 1
    assert "duplicate in ledger: D-001" in res.stdout

def test_void_resolves_duplicate(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text(
        "D-173\t2026-09-03T00:00:00Z\tseed\tseeded from registry\n"
        "D-173\t2026-09-03T00:01:00Z\tuser\tmanual assignment\n",
        encoding="utf-8"
    )
    
    res_before = subprocess.run([sys.executable, "-m", "id_alloc", "check"], env=env, capture_output=True, text=True)
    assert res_before.returncode == 1
    assert "duplicate in ledger: D-173" in res_before.stdout
    
    # Void the duplicate
    subprocess.run([sys.executable, "-m", "id_alloc", "void", "D-173", "--reason", "duplicate of T-999"], env=env, check=True)
    
    res_after = subprocess.run([sys.executable, "-m", "id_alloc", "check"], env=env, capture_output=True, text=True)
    assert res_after.returncode == 0
    assert "duplicate in ledger: D-173" not in res_after.stdout
    
    # Still counted for max
    res_next = subprocess.run([sys.executable, "-m", "id_alloc", "next", "D", "--intent", "next test"], env=env, capture_output=True, text=True, check=True)
    assert res_next.stdout.strip() == "D-174"

def test_d173_d174_hand_written_duplicate_not_reissued(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text(
        "D-173\t2026-08-20T14:32:11Z\tseed\tseeded from registry\n"
        "D-174\t2026-08-20T14:32:11Z\tseed\tseeded from registry\n"
        "D-173\t2026-08-21T03:10:00Z\tuser\tmanual assignment\n"
        "D-174\t2026-08-22T01:15:00Z\tuser\tmanual assignment\n",
        encoding="utf-8"
    )
    
    res_check = subprocess.run([sys.executable, "-m", "id_alloc", "check"], env=env, capture_output=True, text=True)
    assert res_check.returncode == 1
    assert "duplicate in ledger: D-173" in res_check.stdout
    assert "duplicate in ledger: D-174" in res_check.stdout
    
    res_next = subprocess.run([sys.executable, "-m", "id_alloc", "next", "D", "--intent", "next test"], env=env, capture_output=True, text=True, check=True)
    assert res_next.stdout.strip() == "D-175"

def test_seed_covers_all_allowed_prefixes(isolated_paths):
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_lines = [f"- {prefix}-501 — test\n" for prefix in id_alloc.ALLOWED_PREFIXES]
    registry_path.write_text("".join(registry_lines), encoding="utf-8")
    
    subprocess.run([sys.executable, "-m", "id_alloc", "seed"], env=env, check=True)
    
    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_content = ledger_path.read_text(encoding="utf-8")
    for prefix in id_alloc.ALLOWED_PREFIXES:
        assert f"{prefix}-501" in ledger_content

    bytes_first = ledger_path.read_bytes()

    subprocess.run([sys.executable, "-m", "id_alloc", "seed"], env=env, check=True)
    bytes_second = ledger_path.read_bytes()

    assert bytes_first == bytes_second

def test_who_column_is_canonicalized(isolated_paths):
    # manager@-github / manager @-github / manager-@-github are three
    # spellings of one actor (T-915 phase C). New writes canonicalize; old
    # ledger rows are never rewritten (ids/rows locked forever).
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    variants = ["manager@-github", "manager @-github", "manager-@-github"]
    for who in variants:
        subprocess.run(
            [sys.executable, "-m", "id_alloc", "next", "D", "--intent", "who test", "--who", who],
            env=env, check=True,
        )

    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        who_field = line.split("\t")[2]
        assert who_field == "manager@-github"

def test_atomic_write_survives_a_ledger_that_already_exists(isolated_paths):
    # Regression for the temp+os.replace() rewrite (T-915 phase C): the
    # atomic write must preserve every prior row, not just append blindly to
    # whatever the fd happened to be positioned at.
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir

    ledger_path = isolated_paths / "ID-LEDGER.tsv"
    ledger_path.write_text("D-001\t2026-09-03T00:00:00Z\ttest\tpre-existing row\n", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, "-m", "id_alloc", "next", "D", "--intent", "second row"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert res.stdout.strip() == "D-002"

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("D-001\t")
    assert lines[1].startswith("D-002\t")

