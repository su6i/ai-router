import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Match existing tests/ sys.path conventions
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from delegate import MODELS  # noqa: E402
except ImportError:
    from src.delegate import MODELS  # noqa: E402


def test_registry_has_newest_generation():
    assert any(k.startswith("gemini-3.7-flash-") for k in MODELS), "Missing gemini-3.7-flash-* models"


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
    assert "gemini-3.7-flash-high" in r.stdout, "Live agy CLI does not mention gemini-3.7-flash-high"
