import sys
from pathlib import Path
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d
import repo_map

def test_context_discipline_worker_prompt():
    task = "fix it"
    files = [("src/main.py", "print('hello')")]
    
    prompt = d.build_worker_prompt(task, files)
    
    assert prompt.startswith("=== CONTEXT DISCIPLINE ===")
    assert "never re-read an unchanged file" in prompt
    
    preamble_idx = prompt.find("=== CONTEXT DISCIPLINE ===")
    map_idx = prompt.find("=== REPO MAP ===")
    files_idx = prompt.find("===CURRENT FILE:")
    
    assert preamble_idx == 0
    assert map_idx > preamble_idx
    assert files_idx > map_idx

def test_context_discipline_agent_delegate_prompt(monkeypatch, tmp_path):
    passed_args = []
    
    class MockPopen:
        def __init__(self, args, **kwargs):
            passed_args.extend(args)
            self.returncode = 0
        def wait(self, timeout=None):
            return 0
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def communicate(self, *args, **kwargs):
            return ("", "")
    
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    
    monkeypatch.setattr(repo_map, "generate_repo_map", lambda *a, **k: "=== REPO MAP ===\n================\n")
    monkeypatch.setattr(d.subprocess, "Popen", MockPopen)
    monkeypatch.setattr(d, "_write_agent_audit", lambda *a, **k: None)
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    monkeypatch.setattr(d, "check_budget", lambda *a, **k: None)
    
    d.agent_delegate("agent task", runner="agy", workdir=tmp_path)
    
    idx = passed_args.index("-p")
    task_arg = passed_args[idx + 1]
    
    assert task_arg.startswith("=== CONTEXT DISCIPLINE ===")
    assert "=== REPO MAP ===" in task_arg
    
    preamble_idx = task_arg.find("=== CONTEXT DISCIPLINE ===")
    map_idx = task_arg.find("=== REPO MAP ===")
    task_idx = task_arg.find("Task:\nagent task")
    
    assert preamble_idx == 0
    assert map_idx > preamble_idx
    assert task_idx > map_idx

def test_repo_map_constraints():
    rmap = repo_map.generate_repo_map(cwd=str(Path(__file__).parent.parent))
    assert len(rmap) <= 4000
    assert "build_worker_prompt" in rmap

def test_template_file_contains_rules():
    tmpl = Path(__file__).parent.parent / "templates" / "AGENTS-context-discipline.md"
    assert tmpl.exists()
    content = tmpl.read_text().lower()
    assert "read a file once" in content
    assert "grep -n" in content
    assert "batch related reads" in content
    assert "one wo phase per session" in content
    assert "never paste large file" in content
    assert "report tokens" in content
