"""Tests for DeepSeek pricing, peak windows, and model aliases (T-992).

DeepSeek V4.1 Flash effective 2026-09-10 04:00 UTC:
- Off-peak: cin 0.15, cin_cached 0.003, cout 0.60
- Peak windows: UTC 01:00-04:00 and 06:00-10:00, Monday-Friday only (2x multiplier)
- API model ID is 'deepseek-flash' (not 'deepseek-v4-flash')
"""
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


# ---- 8a: deepseek_is_peak -----------------------------------------------------

def test_deepseek_is_peak_weekday_in_windows():
    # Monday (weekday 0)
    assert d.deepseek_is_peak(datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)) is True
    assert d.deepseek_is_peak(datetime(2026, 9, 7, 3, 59, tzinfo=timezone.utc)) is True
    # Wednesday (weekday 2)
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)) is True
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 9, 30, tzinfo=timezone.utc)) is True
    # Friday (weekday 4)
    assert d.deepseek_is_peak(datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)) is True
    assert d.deepseek_is_peak(datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)) is True


def test_deepseek_is_peak_weekday_outside_windows():
    # Wednesday (weekday 2)
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 4, 30, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 5, 59, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)) is False


def test_deepseek_is_peak_weekend_always_false():
    # Saturday (weekday 5) - even inside window hours, must be False
    assert d.deepseek_is_peak(datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)) is False
    # Sunday (weekday 6) - even inside window hours, must be False
    assert d.deepseek_is_peak(datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 13, 7, 0, tzinfo=timezone.utc)) is False


def test_deepseek_is_peak_boundary_hours():
    # Windows are [1, 4) and [6, 10)
    # Thursday (weekday 3)
    assert d.deepseek_is_peak(datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)) is True   # start of [1, 4)
    assert d.deepseek_is_peak(datetime(2026, 9, 10, 3, 59, tzinfo=timezone.utc)) is True
    assert d.deepseek_is_peak(datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)) is False  # end of [1, 4)
    assert d.deepseek_is_peak(datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)) is True   # start of [6, 10)
    assert d.deepseek_is_peak(datetime(2026, 9, 10, 9, 59, tzinfo=timezone.utc)) is True
    assert d.deepseek_is_peak(datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)) is False # end of [6, 10)


def test_deepseek_is_peak_naive_datetime():
    # Pure function must accept naive UTC datetimes as well
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 2, 0)) is True
    assert d.deepseek_is_peak(datetime(2026, 9, 9, 12, 0)) is False
    assert d.deepseek_is_peak(datetime(2026, 9, 13, 2, 0)) is False


# ---- 8b: compute_token_cost peak multiplier ----------------------------------

def test_compute_token_cost_deepseek_peak_multiplier():
    spec_flash = d.MODELS["flash"]
    spec_pro = d.MODELS["pro"]
    pin, pout, cached = 1_000_000, 100_000, 200_000

    wed_peak = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
    wed_offpeak = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    # Flash: peak cost is exactly 2x off-peak cost
    cost_flash_off = d.compute_token_cost(spec_flash, pin, pout, cached, now_utc=wed_offpeak)
    cost_flash_peak = d.compute_token_cost(spec_flash, pin, pout, cached, now_utc=wed_peak)
    assert cost_flash_peak == pytest.approx(cost_flash_off * d.DEEPSEEK_OFF_PEAK_MULTIPLIER)
    assert cost_flash_peak == pytest.approx(cost_flash_off * 2.0)

    # Pro: peak cost is exactly 2x off-peak cost
    cost_pro_off = d.compute_token_cost(spec_pro, pin, pout, cached, now_utc=wed_offpeak)
    cost_pro_peak = d.compute_token_cost(spec_pro, pin, pout, cached, now_utc=wed_peak)
    assert cost_pro_peak == pytest.approx(cost_pro_off * d.DEEPSEEK_OFF_PEAK_MULTIPLIER)
    assert cost_pro_peak == pytest.approx(cost_pro_off * 2.0)


def test_compute_token_cost_non_deepseek_not_multiplied_during_peak():
    spec_grok = d.MODELS["grok"]
    spec_minimax = d.MODELS["minimax"]
    pin, pout, cached = 100_000, 10_000, 20_000

    wed_peak = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
    wed_offpeak = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    cost_grok_peak = d.compute_token_cost(spec_grok, pin, pout, cached, now_utc=wed_peak)
    cost_grok_off = d.compute_token_cost(spec_grok, pin, pout, cached, now_utc=wed_offpeak)
    assert cost_grok_peak == pytest.approx(cost_grok_off)

    cost_mm_peak = d.compute_token_cost(spec_minimax, pin, pout, cached, now_utc=wed_peak)
    cost_mm_off = d.compute_token_cost(spec_minimax, pin, pout, cached, now_utc=wed_offpeak)
    assert cost_mm_peak == pytest.approx(cost_mm_off)


# ---- 8d: alias resolution and catalog assertions ------------------------------

def test_alias_resolution_and_catalog():
    assert d.resolve_model("deepseek-v4-flash") == "flash"
    assert d.resolve_model("deepseek-v4.1-flash") == "flash"
    assert d.resolve_model("deepseek-flash") == "flash"
    assert d.MODELS["flash"]["api"] == "deepseek-flash"
    assert d.MODELS["pro"]["api"] == "deepseek-v4-pro"


def test_deepseek_off_peak_prices_in_models():
    for m in ("flash", "pro"):
        spec = d.MODELS[m]
        assert spec["cin"] == 0.15
        assert spec["cin_cached"] == 0.003
        assert spec["cout"] == 0.60
