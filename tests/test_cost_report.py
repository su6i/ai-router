"""Tests for cost report (--cost)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "AUDIT", tmp_path / "audit.log")
    yield


def test_cost_report_empty_audit(capsys):
    d.show_cost()
    assert "(no audit.log yet)" in capsys.readouterr().out


def test_cost_report_table_output(tmp_path, capsys):
    lines = [
        {"ts": "2026-07-11T12:00:00+00:00", "model_asked": "flash", "cost_usd": 0.010, "in": 100, "out": 200, "cache": 0, "mode": "chat", "cached": False},
        {"ts": "2026-07-11T12:05:00+00:00", "model_asked": "flash", "cost_usd": 0.005, "in": 100, "out": 50, "cache": 50, "mode": "chat", "cached": False},
        {"ts": "2026-07-11T12:10:00+00:00", "model_asked": "pro", "cost_usd": 0.020, "in": 50, "out": 100, "cache": 0, "mode": "chat", "cached": True},
        {"ts": "2026-07-11T12:15:00+00:00", "model_asked": "pro", "cost_usd": 0.010, "mode": "worker", "cached": False},
        "not a json line",
    ]
    with open(tmp_path / "audit.log", "w") as f:
        for line in lines:
            if isinstance(line, dict):
                f.write(json.dumps(line) + "\n")
            else:
                f.write(line + "\n")

    d.show_cost(by="model")
    out = capsys.readouterr().out
    
    assert "skipped 1 malformed lines" in out
    
    # Check table headers
    assert "group" in out and "hit_rate" in out
    
    # Check group rows
    assert "flash" in out
    assert "pro" in out
    
    # flash totals: 2 calls, in=200, out=250, cache=50 -> hit rate 25.0%
    assert "25.0%" in out
    
    # pro totals: 2 calls, in=50, out=100 (from chat), worker doesn't add to tokens
    
    # Check TOTAL row
    assert "TOTAL" in out
    assert " 4 " in out # total calls
    assert "0.045000" in out # 0.010 + 0.005 + 0.020 + 0.010

def test_cost_report_since_filtering(tmp_path, capsys):
    lines = [
        {"ts": "2026-07-10T12:00:00+00:00", "model_asked": "flash", "cost_usd": 1.0, "in": 10, "out": 10},
        {"ts": "2026-07-11T12:00:00+00:00", "model_asked": "flash", "cost_usd": 2.0, "in": 10, "out": 10},
    ]
    with open(tmp_path / "audit.log", "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    d.show_cost(since="2026-07-11", by="model")
    out = capsys.readouterr().out
    
    assert "2.000000" in out
    assert "3.000000" not in out # Excludes the first line


def test_equiv_cost_never_leaks_into_the_cost_report(tmp_path, capsys):
    """`cost_usd_equiv` is a comparison number, not money (D-233).

    A subscription call carries a large equivalent price next to a real
    `cost_usd` of 0.0. The report — and every budget decision downstream of
    it — must see only the 0.0. Asserting the totals here is what stops a
    later refactor from quietly summing the shadow column and reporting spend
    that never happened.
    """
    lines = [
        {"ts": "2026-09-04T12:00:00+02:00", "model_asked": "gemini-3.8-flash-high",
         "cost_usd": 0.0, "cost_usd_equiv": 12.5, "cost_equiv_basis": "gemini-3.8-flash",
         "in": 1000, "out": 500, "cache": 0, "mode": "chat", "cached": False,
         "quota_channel": "google-ai-pro-gemini"},
        {"ts": "2026-09-04T12:05:00+02:00", "model_asked": "flash",
         "cost_usd": 0.25, "in": 100, "out": 50, "cache": 0, "mode": "chat", "cached": False},
    ]
    d.AUDIT.write_text("".join(json.dumps(x) + "\n" for x in lines))

    d.show_cost()
    out = capsys.readouterr().out

    assert "12.5" not in out and "12.500000" not in out
    total_line = [ln for ln in out.splitlines() if ln.startswith("TOTAL")][0]
    assert "0.250000" in total_line
