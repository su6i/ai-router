import json
import logging
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

# Make sure we can import delegate
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import delegate


@pytest.fixture
def isolated_vault(tmp_path):
    with patch("delegate.VAULT", tmp_path), \
         patch("delegate.DATA_DIR", tmp_path / "data"), \
         patch("delegate.AUDIT", tmp_path / "data" / "audit.log"), \
         patch("delegate.BUDGETS", tmp_path / "data" / "budgets.json"), \
         patch("delegate.project_info", return_value=("testproj", "abc")):
        (tmp_path / "data").mkdir()
        yield tmp_path


def test_budget_no_file(isolated_vault, caplog):
    caplog.set_level(logging.INFO)
    # Should print a warning and not crash
    delegate.check_budget("testproj", "session1", print_estimate=False)
    assert "no budgets.json — spend uncapped" in caplog.text


def _pin_now(monkeypatch, iso):
    """Pin check_budget's idea of "now" so the month/day windows do not depend
    on the calendar date — or the timezone — the suite happens to run under."""
    pinned = delegate.dt.datetime.fromisoformat(iso)
    monkeypatch.setattr(delegate, "_now_local", lambda: pinned)
    return pinned


def test_budget_under_cap(isolated_vault, caplog, monkeypatch):
    # Pinned into the row's own month: without this the row falls outside the
    # window and the assertion below holds for the wrong reason.
    _pin_now(monkeypatch, "2026-07-14T12:00:00+02:00")
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
        "per_project_monthly_usd": {"testproj": 1.0}
    }))
    delegate.AUDIT.write_text(json.dumps({
        "ts": "2026-07-14T00:00:00+00:00",
        "cost_usd": 0.5,
        "project": "testproj"
    }) + "\n")

    # Should not raise
    delegate.check_budget("testproj", "session1")
    assert "BUDGET WARNING" not in caplog.text


def test_budget_warning(isolated_vault, caplog):
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
    }))
    delegate.AUDIT.write_text(json.dumps({
        "ts": delegate.dt.datetime.now().astimezone().isoformat(),
        "cost_usd": 4.1,
    }) + "\n")

    delegate.check_budget("testproj", "session1")
    assert "BUDGET WARNING: monthly_usd spend at" in caplog.text


def test_budget_abort(isolated_vault):
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
    }))
    delegate.AUDIT.write_text(json.dumps({
        "ts": delegate.dt.datetime.now().astimezone().isoformat(),
        "cost_usd": 5.1,
    }) + "\n")

    with pytest.raises(SystemExit) as exc:
        delegate.check_budget("testproj", "session1")
    assert "BUDGET ABORT: monthly_usd cap exceeded" in str(exc.value)


def test_budget_project_abort(isolated_vault):
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
        "per_project_monthly_usd": {"testproj": 1.0}
    }))
    delegate.AUDIT.write_text(json.dumps({
        "ts": delegate.dt.datetime.now().astimezone().isoformat(),
        "cost_usd": 1.5,
        "project": "testproj"
    }) + "\n")

    with pytest.raises(SystemExit) as exc:
        delegate.check_budget("testproj", "session1")
    assert "BUDGET ABORT: per_project_monthly_usd[testproj] cap exceeded" in str(exc.value)


def test_budget_free_model_proceeds(isolated_vault, caplog):
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
    }))
    delegate.AUDIT.write_text(json.dumps({
        "ts": delegate.dt.datetime.now().astimezone().isoformat(),
        "cost_usd": 5.1,
    }) + "\n")

    # Should NOT raise SystemExit for a FREE model (cin=0, cout=0)
    free_spec = {"cin": 0.0, "cout": 0.0}
    delegate.check_budget("testproj", "session1", model_spec=free_spec)
    
    assert "BUDGET WARNING: monthly_usd cap exceeded" in caplog.text
    assert "proceeding because model is FREE" in caplog.text


@patch("delegate.call_openai")
@patch("delegate.call_gemini")
def test_estimate_flag_no_calls(mock_gemini, mock_openai, isolated_vault, capsys):
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
    }))
    os.environ["MINIMAX_API_KEY"] = "fake"
    
    with pytest.raises(SystemExit) as exc:
        delegate.delegate("prompt", "minimax", estimate=True)
    
    assert exc.value.code == 0
    mock_gemini.assert_not_called()
    mock_openai.assert_not_called()

    out = capsys.readouterr().out
    assert "ESTIMATE for minimax" in out
    assert "Cost USD" in out
    assert "monthly_usd: $0.000000 / $5.00" in out


def test_budget_counts_row_stamped_in_another_offset(isolated_vault, monkeypatch):
    # A UTC writer stamps the same instant as 2026-08-31T22:30+00:00 while the
    # local calendar already says September. Matching the month by string
    # prefix files that row under August, and a cap that is already blown
    # reads as $0 spent.
    _pin_now(monkeypatch, "2026-09-01T00:30:00+02:00")
    delegate.BUDGETS.write_text(json.dumps({"monthly_usd": 1.0}))
    delegate.AUDIT.write_text(json.dumps({
        "ts": "2026-08-31T22:30:00+00:00",
        "cost_usd": 1.5,
    }) + "\n")

    with pytest.raises(SystemExit) as exc:
        delegate.check_budget("testproj", "session1")
    assert "BUDGET ABORT: monthly_usd cap exceeded" in str(exc.value)


def test_budget_excludes_previous_month_across_offsets(isolated_vault, monkeypatch):
    # The mirror image: a row that genuinely belongs to last month must stay
    # out of the window, so the fix cannot be "count everything".
    _pin_now(monkeypatch, "2026-09-01T23:30:00+02:00")
    delegate.BUDGETS.write_text(json.dumps({"monthly_usd": 1.0}))
    delegate.AUDIT.write_text(json.dumps({
        "ts": "2026-08-31T23:30:00+02:00",
        "cost_usd": 1.5,
    }) + "\n")

    delegate.check_budget("testproj", "session1")  # must not abort


def test_budget_warns_on_unreadable_ts(isolated_vault, caplog, monkeypatch):
    # A row in no window at all lowers the apparent spend; that must be said
    # out loud rather than silently shrinking the cap's view.
    _pin_now(monkeypatch, "2026-09-01T00:30:00+02:00")
    delegate.BUDGETS.write_text(json.dumps({"monthly_usd": 1.0}))
    delegate.AUDIT.write_text(json.dumps({
        "ts": "not-a-timestamp",
        "cost_usd": 1.5,
    }) + "\n")

    delegate.check_budget("testproj", "session1")
    assert "unreadable ts" in caplog.text
