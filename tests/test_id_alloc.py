import os
import subprocess
import sys
from pathlib import Path

import pytest

src_dir = str(Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, src_dir)

import id_alloc

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
    # Test case 2 & 3: registry has D-200, ledger empty -> next D is D-201
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text("Here is a manually added ID: D-200\n", encoding="utf-8")
    
    env = os.environ.copy()
    env["AGENT_MEMORY_DIR"] = str(isolated_paths)
    env["PYTHONPATH"] = src_dir
    
    # 2. next D returns D-201
    cmd_next = [sys.executable, "-m", "id_alloc", "next", "D", "--intent", "test_offset"]
    res_next = subprocess.run(cmd_next, env=env, capture_output=True, text=True, check=True)
    assert res_next.stdout.strip() == "D-201"
    
    # 3. check should exit 1 and report D-200 as manually assigned
    cmd_check = [sys.executable, "-m", "id_alloc", "check"]
    res_check = subprocess.run(cmd_check, env=env, capture_output=True, text=True)
    assert res_check.returncode == 1
    assert "manually assigned: D-200" in res_check.stdout

def test_seed_deduplication(isolated_paths):
    # Test case 4: fake registry with 3 occurrences of T-162 -> seed -> exactly 1 line
    registry_path = isolated_paths / "REGISTRY-IDS.md"
    registry_path.write_text("T-162 first\nT-162 second\nT-162 third", encoding="utf-8")
    
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
