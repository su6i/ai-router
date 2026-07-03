"""Tests for the r() shell wrapper (shell/r.sh).

The wrapper is exercised through real shells (bash and, when installed, zsh)
against a stub delegate.py that prints the argv it received as JSON — no
provider is ever called, no network, zero cost.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "shell" / "r.sh"

STUB = """\
import json, os, sys
print(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""

SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


@pytest.fixture()
def fake_repo(tmp_path):
    """A minimal repo layout whose src/delegate.py records its argv."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "delegate.py").write_text(STUB)
    return tmp_path


def run_r(shell, fake_repo, args, extra_env=None):
    env = dict(os.environ, AI_ROUTER_REPO=str(fake_repo))
    env.update(extra_env or {})
    script = f'source "{WRAPPER}" && r {args}'
    return subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, env=env
    )


@pytest.mark.parametrize("shell", SHELLS)
def test_chat_words_join_into_single_prompt(shell, fake_repo):
    proc = run_r(shell, fake_repo, "flash hello world")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["--model", "flash", "-p", "hello world"]


@pytest.mark.parametrize("shell", SHELLS)
def test_leading_dash_passes_args_through_unchanged(shell, fake_repo):
    proc = run_r(
        shell,
        fake_repo,
        'gemini --files a.py --allow-write "src/**" -p "fix it"',
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "--model", "gemini",
        "--files", "a.py",
        "--allow-write", "src/**",
        "-p", "fix it",
    ]


@pytest.mark.parametrize("shell", SHELLS)
def test_audit_subcommand(shell, fake_repo):
    proc = run_r(shell, fake_repo, "audit")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["--audit"]


@pytest.mark.parametrize("shell", SHELLS)
def test_no_args_prints_usage_and_never_calls_delegate(shell, fake_repo):
    proc = run_r(shell, fake_repo, "")
    assert proc.returncode == 2
    assert proc.stdout == ""  # stub never ran
    assert "usage:" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_model_without_prompt_is_refused_without_paid_call(shell, fake_repo):
    proc = run_r(shell, fake_repo, "flash")
    assert proc.returncode == 2
    assert proc.stdout == ""  # stub never ran
    assert "usage:" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_delegate_exit_code_propagates(shell, fake_repo):
    proc = run_r(shell, fake_repo, "flash hi", extra_env={"STUB_EXIT": "3"})
    assert proc.returncode == 3


def test_both_shells_are_actually_tested():
    assert "bash" in SHELLS  # bash is always present on macOS/Linux
