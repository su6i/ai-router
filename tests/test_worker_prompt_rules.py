import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


@pytest.fixture(autouse=True)
def setup_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "_WORKER_RULES_CACHE", {})
    monkeypatch.setattr(d, "_worker_rules_truncated_warned", False)
    monkeypatch.setattr(d, "AGENT_PROJECTS", tmp_path)


def test_rules_file_injected(tmp_path):
    memory_dir = tmp_path / "_memory"
    memory_dir.mkdir(parents=True)
    rules_file = memory_dir / "WORKER-RULES.md"
    rules_file.write_text("SENTINEL-SHOULD-APPEAR-abc123")

    prompt = d.build_worker_prompt("t", [])
    assert "SENTINEL-SHOULD-APPEAR-abc123" in prompt


def test_rules_file_absent(tmp_path):
    prompt = d.build_worker_prompt("some task", [])
    assert "some task" in prompt
    assert "Standing worker rules" not in prompt


def test_rules_file_truncated(tmp_path):
    memory_dir = tmp_path / "_memory"
    memory_dir.mkdir(parents=True)
    rules_file = memory_dir / "WORKER-RULES.md"
    cap = d.WORKER_RULES_MAX_CHARS
    rules_file.write_text("X" * (cap + 5000))

    prompt = d.build_worker_prompt("t", [])

    assert "… (truncated)" in prompt
    # Cap-relative on purpose: the constant is a safety valve that may be raised
    # as WORKER-RULES.md grows, and this test must keep testing truncation
    # rather than a frozen number.
    assert "X" * cap + "\n… (truncated)" in prompt
    assert "X" * (cap + 1) not in prompt


def test_real_rules_file_fits_without_truncation():
    """The live WORKER-RULES.md must fit whole; silent truncation of it is the
    bug this cap was raised to fix."""
    real = Path.home() / ".local/share/agent-projects/_memory/WORKER-RULES.md"
    if not real.exists():
        pytest.skip("vault not mounted")
    assert len(real.read_text().strip()) <= d.WORKER_RULES_MAX_CHARS, (
        "WORKER-RULES.md outgrew WORKER_RULES_MAX_CHARS — raise the cap or trim "
        "the file; do not let workers receive a silently truncated ruleset."
    )


def test_rules_file_read_once_per_process(tmp_path, monkeypatch):
    memory_dir = tmp_path / "_memory"
    memory_dir.mkdir(parents=True)
    rules_file = memory_dir / "WORKER-RULES.md"
    rules_file.write_text("some rules")

    read_count = 0
    original_read_text = Path.read_text

    def mock_read_text(self, *args, **kwargs):
        nonlocal read_count
        if self == rules_file:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mock_read_text)

    d.build_worker_prompt("t1", [])
    d.build_worker_prompt("t2", [])

    assert read_count == 1
