import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

# Match existing tests/ sys.path conventions
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from delegate import MODELS  # noqa: E402
except ImportError:
    from src.delegate import MODELS  # noqa: E402


def parse_agy_models_stdout(stdout: str) -> set[str]:
    """Extract model ids from `agy models` CLI stdout.

    The model id is the first whitespace-delimited token on each row.
    Header lines (e.g. "Fetching available models...") and blank lines
    are skipped.
    """
    ids = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line == "Fetching available models..." or "\t" not in line:
            continue
        ids.add(line.split()[0])
    return ids


_SAMPLE_AGY_MODELS_STDOUT = """Fetching available models...
gemini-3.7-flash-high\tGemini 3.7 Flash (High)
gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
gemini-3.6-flash-high\tGemini 3.6 Flash (High)
gemini-3.6-flash-medium\tGemini 3.6 Flash (Medium)
gemini-3.6-flash-low\tGemini 3.6 Flash (Low)
gemini-3.1-pro-high\tGemini 3.1 Pro (High)
gemini-3.1-pro-low\tGemini 3.1 Pro (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


def test_parse_agy_models_stdout():
    ids = parse_agy_models_stdout(_SAMPLE_AGY_MODELS_STDOUT)
    assert ids == {
        "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
        "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
        "gemini-3.1-pro-high", "gemini-3.1-pro-low",
        "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
    }
    assert "Fetching" not in "".join(ids)


def test_registry_keys_match_shape():
    valid_patterns = [
        r"minimax",
        r"flash",
        r"pro",
        r"grok",
        r"grok-4\.5",
        r"gemini-3\.\d+-(flash|pro)-(high|medium|low)",
        r"claude-[\w-]+",
        r"gpt-oss-[\w-]+",
    ]
    combined_pattern = re.compile("^(?:" + "|".join(valid_patterns) + ")$")

    mismatched = [k for k in MODELS if not combined_pattern.fullmatch(k)]
    assert not mismatched, f"Keys do not match expected shape: {mismatched}"


@pytest.mark.skipif(shutil.which("agy") is None, reason="agy CLI not installed/offline")
def test_live_channel_agy_models():
    r = subprocess.run(["agy", "models"], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"agy models failed: {r.stderr}"
    served_agy = parse_agy_models_stdout(r.stdout)
    registered_agy = {k for k, v in MODELS.items() if v["provider"] == "agy_cli"}
    missing = served_agy - registered_agy
    assert not missing, f"agy live channel serves models MODELS does not register: {sorted(missing)}"
    extra = registered_agy - served_agy
    if extra:
        warnings.warn(f"MODELS registers agy ids the live CLI no longer serves (registry deletion is an architect decision, not asserted here): {sorted(extra)}")
