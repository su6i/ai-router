import json
import os
import sys
import stat
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CACHE", tmp_path / "cache.db")
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(d, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(d, "BUDGETS", tmp_path / "budgets.json")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")
    
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    yield bin_dir


def create_fake_bin(bin_dir, name, script_content):
    path = bin_dir / name
    path.write_text(script_content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_agent_codewhale_happy_path(isolated_paths, tmp_path, monkeypatch):
    create_fake_bin(isolated_paths, "codewhale", """#!/usr/bin/env python3
import sys
if "metrics" in sys.argv:
    print('{"capacity": {"total": 0}, "cost_usd": 0.05}')
else:
    with open("changed.py", "w") as f:
        f.write("import os\\n")
    print("codewhale stdout")
""")
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("data/\nsessions/\nbudgets.json\ncache.db\naudit.log\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    # The fake codewhale will create changed.py

    
    out = d.agent_delegate("do something", runner="codewhale", model="flash", workdir=tmp_path)
    
    assert "runner        : codewhale (flash)" in out
    assert "status        : COMPLETED" in out
    assert "files changed : 1 files" in out
    assert "changes       : changed.py" in out
    assert "cost          : $0.05" in out
    
    lines = d.AUDIT.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["mode"] == "agent"
    assert rec["runner"] == "codewhale"
    assert rec["files_changed_count"] == 1
    assert rec["quota_channel"] == "deepseek-api"


def test_agent_timeout_kills_process(isolated_paths, tmp_path):
    create_fake_bin(isolated_paths, "agy", """#!/usr/bin/env python3
import time
time.sleep(10)
""")
    out = d.agent_delegate("sleep", runner="agy", workdir=tmp_path, timeout_s=1)
    assert "TIMEOUT" in out


def test_agent_unparseable_metrics_cost_unknown(isolated_paths, tmp_path):
    create_fake_bin(isolated_paths, "codewhale", """#!/usr/bin/env python3
import sys
if "metrics" in sys.argv:
    print('{"broken json')
else:
    print("codewhale stdout")
""")
    out = d.agent_delegate("do something", runner="codewhale", workdir=tmp_path)
    assert "cost          : unknown" in out
    lines = d.AUDIT.read_text().strip().splitlines()
    assert json.loads(lines[0])["cost_unknown"] is True


def test_agent_agy_gets_print_timeout_and_default_model(isolated_paths, tmp_path):
    # agy print mode dies at --print-timeout (default 5m); every launch must
    # pass it explicitly, sized to our own timeout, and never leak a chat
    # default like "minimax" into agy's --model.
    argv_log = tmp_path / "argv.json"
    create_fake_bin(isolated_paths, "agy", f"""#!/usr/bin/env python3
import json, sys
open({str(argv_log)!r}, "w").write(json.dumps(sys.argv))
""")
    out = d.agent_delegate("task", runner="agy", workdir=tmp_path, timeout_s=120)
    assert "COMPLETED" in out
    argv = json.loads(argv_log.read_text())
    assert "--print-timeout" in argv
    assert argv[argv.index("--print-timeout") + 1] == "120s"
    assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (High)"


def test_agent_runner_failure_reported_loudly(isolated_paths, tmp_path):
    create_fake_bin(isolated_paths, "agy", """#!/usr/bin/env python3
import sys
print("Error: timeout waiting for response")
sys.exit(1)
""")
    out = d.agent_delegate("task", runner="agy", workdir=tmp_path)
    assert "FAILED (exit 1)" in out
    assert "COMPLETED" not in out
    rec = json.loads(d.AUDIT.read_text().strip().splitlines()[0])
    assert rec["runner_exit"] == 1


def test_agent_claude_models_banned(isolated_paths, tmp_path):
    with pytest.raises(ValueError, match="banned"):
        d.agent_delegate("task", runner="agy", model="Claude Sonnet 4.5", workdir=tmp_path)


def test_agent_daily_call_cap_aborts(isolated_paths, tmp_path):
    create_fake_bin(isolated_paths, "agy", """#!/usr/bin/env python3
print("ok")
""")
    d.BUDGETS.write_text(json.dumps({"daily_calls": {"google-ai-pro": 1}}))
    out = d.agent_delegate("first", runner="agy", workdir=tmp_path)
    assert "COMPLETED" in out
    with pytest.raises(SystemExit) as e:
        d.agent_delegate("second", runner="agy", workdir=tmp_path)
    assert "google-ai-pro" in str(e.value)
