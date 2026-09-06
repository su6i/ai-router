import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


def test_agy_cli_real_observed_pair_is_sane():
    # T-958: real ledger row from 2026-09-06 that used to report 977.2%
    # under the old cache/pin formula (1363302 / 139518 = 9.772).
    rate_str, warning = d._compute_cache_hit_rate(cache=1363302, pin=139518, provider="agy_cli")
    assert warning is None
    assert rate_str == "90.7%"


def test_agy_cli_normal_pair():
    rate_str, warning = d._compute_cache_hit_rate(cache=200, pin=800, provider="agy_cli")
    assert warning is None
    assert rate_str == "20.0%"


def test_openai_compat_normal_pair_unchanged():
    # DeepSeek/MiniMax: prompt_tokens already includes the cached subset,
    # so this formula (and its result) must be identical to before the fix.
    rate_str, warning = d._compute_cache_hit_rate(cache=200, pin=1000, provider="openai")
    assert warning is None
    assert rate_str == "20.0%"


def test_zero_denominator_is_safe():
    rate_str, warning = d._compute_cache_hit_rate(cache=0, pin=0, provider="agy_cli")
    assert rate_str == "n/a"
    assert warning is None

    rate_str, warning = d._compute_cache_hit_rate(cache=0, pin=0, provider="openai")
    assert rate_str == "n/a"
    assert warning is None


def test_out_of_range_guard_fires():
    # openai-compat path where cache > pin should never happen for a well
    # behaved provider, but a malformed/buggy usage object must not print a
    # nonsense percentage.
    rate_str, warning = d._compute_cache_hit_rate(cache=200, pin=100, provider="openai")
    assert rate_str == "n/a"
    assert warning is not None
    assert "out of range" in warning
