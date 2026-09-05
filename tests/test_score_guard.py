"""Tests for the T-949 score-ledger guard: a delegated worker must not be able
to grade its own output, and an unsigned verdict must never enter the ledger.

Host-independent by construction: every ledger this file reads/writes lives in
tmp_path, and `AI_ROUTER_REVIEWER`/`AI_ROUTER_IN_WORKER` are always set or
unset explicitly per test via monkeypatch (see the autouse fixture in
tests/conftest.py that strips both from the inherited host environment).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate
import scorecard as sc


def _write_ledger(path, records):
    """Write records as JSON lines and return the path."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# --- record_review_score(): AI_ROUTER_REVIEWER is mandatory --------------

def test_record_review_score_rejects_unset_reviewer(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_ROUTER_REVIEWER", raising=False)
    audit_file = tmp_path / "audit.log"
    with pytest.raises(ValueError) as exc_info:
        sc.record_review_score(audit_file, model="model-a", quality=3, note="no reviewer")
    assert "AI_ROUTER_REVIEWER" in str(exc_info.value)
    assert not audit_file.exists()


def test_record_review_score_rejects_whitespace_only_reviewer(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_REVIEWER", "   ")
    audit_file = tmp_path / "audit.log"
    with pytest.raises(ValueError) as exc_info:
        sc.record_review_score(audit_file, model="model-a", quality=3, note="blank reviewer")
    assert "AI_ROUTER_REVIEWER" in str(exc_info.value)
    assert not audit_file.exists()


# --- record_review_score(): a signed write carries `by` end to end -------

def test_record_review_score_signed_write_carries_by(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_REVIEWER", "some-test-reviewer-id")
    audit_file = tmp_path / "audit.log"
    rec = sc.record_review_score(audit_file, model="model-a", quality=4, note="looks good")
    assert rec["by"] == "some-test-reviewer-id"

    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["by"] == "some-test-reviewer-id"


def test_cli_score_refuses_without_reviewer(monkeypatch):
    """With AI_ROUTER_IN_WORKER unset and AI_ROUTER_REVIEWER unset, --score
    must still reach record_review_score and surface ITS ValueError as a
    SystemExit naming AI_ROUTER_REVIEWER (not some other failure)."""
    monkeypatch.delenv("AI_ROUTER_IN_WORKER", raising=False)
    monkeypatch.delenv("AI_ROUTER_REVIEWER", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["delegate.py", "--score", "--model", "m", "--quality", "3"],
    )

    with pytest.raises(SystemExit) as exc_info:
        delegate.main()

    assert "AI_ROUTER_REVIEWER" in str(exc_info.value)


# --- show_scorecard(): unsigned/unattributed rows are excluded -----------

def test_show_scorecard_excludes_unsigned_and_unattributed_rows(tmp_path):
    audit_file = _write_ledger(
        tmp_path / "audit.log",
        [
            {"mode": "review", "model_asked": "m", "quality": 4, "by": "reviewer-a"},
            {"mode": "review", "model_asked": "m", "quality": 1, "by": "unattributed"},
            {"mode": "review", "model_asked": "m", "quality": 5},  # no "by" at all
        ],
    )
    out = sc.show_scorecard(audit_file)
    lines = out.splitlines()
    headers = [c.strip() for c in lines[0].split("  ") if c.strip()]
    row = [c.strip() for c in lines[2].split("  ") if c.strip()]
    quality_idx = headers.index("quality")

    # Only the signed row (quality=4) counts -- NOT averaged with the
    # unattributed (1) or by-less (5) rows.
    assert "4.00" in row[quality_idx]
    assert "(n=1)" in row[quality_idx]

    num_unsigned_written = 2  # the "unattributed" row + the no-"by" row above
    assert f"{num_unsigned_written} unsigned verdict(s) excluded" in out


# --- CLI: --score refuses inside a worker session, and without a reviewer -

def test_cli_score_refuses_inside_worker_session(monkeypatch):
    """--score must refuse before even checking --model/--quality when
    AI_ROUTER_IN_WORKER is set, and must never call record_review_score."""
    called = {"n": 0}

    def _must_not_be_called(*args, **kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(sc, "record_review_score", _must_not_be_called)
    monkeypatch.setenv("AI_ROUTER_IN_WORKER", "1")
    monkeypatch.setattr(
        "sys.argv",
        ["delegate.py", "--score", "--model", "m", "--quality", "3"],
    )

    with pytest.raises(SystemExit) as exc_info:
        delegate.main()

    assert "AI_ROUTER_IN_WORKER" in str(exc_info.value)
    assert called["n"] == 0
