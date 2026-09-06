"""Tests for src/scorecard.py — the per-model comparison over the ledger (T-948).

Host-independent by construction: every ledger this file reads is written into
tmp_path by the test itself. No vault, no network, no installed binary.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import scorecard as sc


def _write_ledger(path, records):
    """Write records as JSON lines and return the path."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_quality_range_constants():
    assert (sc.QUALITY_MIN, sc.QUALITY_MAX) == (1, 5)


def test_record_review_score_appends_and_returns_record(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_REVIEWER", "test-reviewer")
    audit_file = tmp_path / "audit.log"
    rec1 = sc.record_review_score(audit_file, model="model-a", quality=4, note="looks good", task="T-100")
    assert rec1["mode"] == "review"
    assert rec1["model_asked"] == "model-a"
    assert rec1["quality"] == 4
    assert rec1["note"] == "looks good"
    assert rec1["task"] == "T-100"
    assert rec1["by"] == "test-reviewer"
    assert "ts" in rec1

    rec2 = sc.record_review_score(audit_file, model="model-b", quality=5, note="great")
    assert rec2["mode"] == "review"
    assert rec2["model_asked"] == "model-b"
    assert rec2["quality"] == 5
    assert rec2["note"] == "great"
    assert rec2["task"] == ""
    assert rec2["by"] == "test-reviewer"
    assert "ts" in rec2

    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == rec1
    assert json.loads(lines[1]) == rec2


def test_record_review_score_creates_parent_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_REVIEWER", "test-reviewer")
    audit_file = tmp_path / "sub" / "deep" / "dir" / "audit.log"
    assert not audit_file.parent.exists()
    rec = sc.record_review_score(audit_file, model="model-a", quality=3, note="testing dirs")
    assert audit_file.parent.is_dir()
    assert audit_file.is_file()
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == rec


@pytest.mark.parametrize("bad", [0, 6, -1, 99])
def test_record_review_score_rejects_out_of_range_quality(tmp_path, bad):
    audit_file = tmp_path / "audit.log"
    with pytest.raises(ValueError) as exc_info:
        sc.record_review_score(audit_file, model="model-a", quality=bad, note="out of range")
    err_msg = str(exc_info.value)
    assert str(sc.QUALITY_MIN) in err_msg
    assert str(sc.QUALITY_MAX) in err_msg
    assert f"{sc.QUALITY_MIN} and {sc.QUALITY_MAX}" in err_msg
    assert not audit_file.exists()


def test_missing_ledger_returns_one_line_message(tmp_path):
    audit_file = tmp_path / "nonexistent.log"
    out = sc.show_scorecard(audit_file)
    assert out == "(no audit.log yet)"
    assert len(out.splitlines()) == 1


def test_malformed_lines_are_skipped_not_raised(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            "not-a-dict",
            12345,
            {"other_key": "no model here"},
            {"model_asked": "valid-model", "verify_status": "PASS"},
        ],
    )
    with audit_file.open("a", encoding="utf-8") as f:
        f.write("{{bad json syntax\n\n")

    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    assert len(lines) >= 3
    assert "valid-model" in out
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]
    assert row[0] == "valid-model"
    assert row[1] == "1"
    assert row[2] == "100.0%"


def test_absent_column_shows_dash_not_zero(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-no-lat-equiv",
                "verify_status": "PASS",
                "attempts": 1,
                "in": 100,
                "out": 50,
                "cost_usd": 0.005,
            }
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    lat_idx = headers.index("avg_latency_s")
    equiv_idx = headers.index("equiv_usd")

    assert row[lat_idx] == "-"
    assert "0" not in row[lat_idx]

    assert row[equiv_idx] == "-"
    assert "0" not in row[equiv_idx]


def test_review_records_feed_quality_only_not_run_count(tmp_path, monkeypatch):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "test-model",
                "verify_status": "PASS",
                "ts": "2026-09-01T10:00:00",
            },
            {
                "mode": "review",
                "model_asked": "test-model",
                "quality": 2,
                "note": "needs work",
                "ts": "2026-09-01T11:00:00",
                "by": "reviewer-x",
            },
        ],
    )
    out_before = sc.show_scorecard(audit_file)
    lines_before = out_before.splitlines()
    headers = [c.strip() for c in lines_before[0].split("  ") if c.strip()]
    row_before = [c.strip() for c in lines_before[2].split("  ") if c.strip()]
    runs_idx = headers.index("runs")
    quality_idx = headers.index("quality")

    assert row_before[runs_idx] == "1"
    assert "2.00" in row_before[quality_idx]
    assert "(n=1)" in row_before[quality_idx]
    avg_before = float(row_before[quality_idx].split()[0])

    monkeypatch.setenv("AI_ROUTER_REVIEWER", "reviewer-y")
    sc.record_review_score(audit_file, model="test-model", quality=5, note="much better now")

    out_after = sc.show_scorecard(audit_file)
    lines_after = out_after.splitlines()
    row_after = [c.strip() for c in lines_after[2].split("  ") if c.strip()]

    assert row_after[runs_idx] == "1"
    assert row_after[runs_idx] == row_before[runs_idx]
    assert "3.50" in row_after[quality_idx]
    assert "(n=2)" in row_after[quality_idx]
    avg_after = float(row_after[quality_idx].split()[0])

    assert avg_after > avg_before
    assert avg_before == 2.0
    assert avg_after == 3.5


def test_since_filters_by_ts_date_prefix(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {"model_asked": "old-model", "ts": "2026-09-01T10:00:00", "verify_status": "PASS"},
            {"model_asked": "new-model", "ts": "2026-09-02T10:00:00", "verify_status": "PASS"},
            {"model_asked": "new-model", "ts": "2026-09-04T10:00:00", "verify_status": "PASS"},
        ],
    )
    all_out = sc.show_scorecard(audit_file)
    assert "old-model" in all_out
    assert "new-model" in all_out

    filtered_out = sc.show_scorecard(audit_file, since="2026-09-03")
    assert "old-model" not in filtered_out
    assert "new-model" in filtered_out

    lines = filtered_out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]
    runs_idx = headers.index("runs")
    assert row[0] == "new-model"
    assert row[runs_idx] == "1"

    future_out = sc.show_scorecard(audit_file, since="2026-09-10")
    assert future_out == "(no scorecard data yet)"


def test_aggregates_runs_verify_rate_attempts_and_tokens(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-x",
                "verify_status": "PASS",
                "attempts": 1,
                "self_fix_rounds": 0,
                "in": 100,
                "out": 20,
            },
            {
                "model_asked": "model-x",
                "verify_status": "FAIL",
                "attempts": 3,
                "self_fix_rounds": 1,
                "in": 200,
                "out": 30,
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    assert row[headers.index("model")] == "model-x"
    assert row[headers.index("runs")] == "2"
    assert row[headers.index("verify_pass")] == "50.0%"
    assert row[headers.index("avg_attempts")] == "2.00"
    assert row[headers.index("self_fix_rate")] == "50.0%"
    assert row[headers.index("in_tokens")] == "300"
    assert row[headers.index("out_tokens")] == "50"


def test_equiv_cost_column_sums_separately_from_real_cost(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-both",
                "cost_usd": 0.001,
                "cost_usd_equiv": 0.004,
            },
            {
                "model_asked": "model-both",
                "cost_usd": 0.002,
                "cost_usd_equiv": 0.005,
            },
            {
                "model_asked": "model-real-only",
                "cost_usd": 0.003,
            },
            {
                "model_asked": "model-equiv-only",
                "cost_usd_equiv": 0.006,
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    real_idx = headers.index("real_usd")
    equiv_idx = headers.index("equiv_usd")

    rows_by_model = {
        r[0]: r for r in ([c.strip() for c in line.split("  ") if c.strip()] for line in lines[2:])
    }

    both_row = rows_by_model["model-both"]
    assert both_row[real_idx] == "0.003000"
    assert both_row[equiv_idx] == "0.009000"
    assert both_row[real_idx] != both_row[equiv_idx]

    real_only_row = rows_by_model["model-real-only"]
    assert real_only_row[real_idx] == "0.003000"
    assert real_only_row[equiv_idx] == "-"

    equiv_only_row = rows_by_model["model-equiv-only"]
    assert equiv_only_row[real_idx] == "-"
    assert equiv_only_row[equiv_idx] == "0.006000"


def test_unverified_percentage_counts_only_code_runs(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-y",
                "mode": "worker",
                "verify_status": "PASS",
                "verify_present": True,
                "ts": "2026-09-01T10:00:00",
            },
            {
                "model_asked": "model-y",
                "mode": "worker",
                "verify_status": "SKIPPED",
                "verify_present": False,
                "ts": "2026-09-01T11:00:00",
            },
            {
                "model_asked": "model-y",
                "mode": "agent",
                "verify_status": "SKIPPED",
                "verify_present": False,
                "ts": "2026-09-01T12:00:00",
            },
            {
                "model_asked": "model-y",
                "cost_usd": 0.01,
                "ts": "2026-09-01T13:00:00",
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    assert row[headers.index("model")] == "model-y"
    assert row[headers.index("runs")] == "4"
    assert row[headers.index("%unverified")] == "66.7%"


def test_unverified_percentage_backward_compat_old_ledger_row(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-legacy",
                "mode": "worker",
                "verify_status": "SKIPPED",
                "ts": "2026-09-01T10:00:00",
            },
            {
                "model_asked": "model-legacy",
                "mode": "worker",
                "verify_status": "PASS",
                "ts": "2026-09-01T11:00:00",
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    assert row[headers.index("model")] == "model-legacy"
    assert row[headers.index("runs")] == "2"
    assert row[headers.index("%unverified")] == "50.0%"


def test_tok_per_run_averages_in_out_and_cache(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-worker",
                "mode": "worker",
                "in": 1000,
                "out": 500,
                "cache": 8500,
            },
            {
                "model_asked": "model-worker",
                "mode": "worker",
                "in": 2000,
                "out": 1000,
                "cache": 17000,
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    tok_run_idx = headers.index("tok/run")
    assert row[tok_run_idx] == "15,000"


def test_tok_per_run_dash_when_no_token_data(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-no-tokens",
                "verify_status": "PASS",
                "attempts": 1,
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    tok_run_idx = headers.index("tok/run")
    assert row[tok_run_idx] == "-"


def test_tok_per_run_with_zero_cache(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {
                "model_asked": "model-zero-cache",
                "in": 300,
                "out": 100,
                "cache": 0,
            },
            {
                "model_asked": "model-zero-cache",
                "in": 500,
                "out": 100,
                "cache": 0,
            },
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]

    tok_run_idx = headers.index("tok/run")
    assert row[tok_run_idx] == "500"
