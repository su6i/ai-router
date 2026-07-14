import json
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


def test_budget_no_file(isolated_vault, capsys):
    # Should print a warning and not crash
    delegate.check_budget("testproj", "session1", print_estimate=False)
    err = capsys.readouterr().err
    assert "no budgets.json — spend uncapped" in err


def test_budget_under_cap(isolated_vault, capsys):
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
    err = capsys.readouterr().err
    assert "BUDGET WARNING" not in err


def test_budget_warning(isolated_vault, capsys):
    delegate.BUDGETS.write_text(json.dumps({
        "monthly_usd": 5.0,
    }))
    delegate.AUDIT.write_text(json.dumps({
        "ts": delegate.dt.datetime.now().astimezone().isoformat(),
        "cost_usd": 4.1,
    }) + "\n")

    delegate.check_budget("testproj", "session1")
    err = capsys.readouterr().err
    assert "BUDGET WARNING: monthly_usd spend at" in err


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


def test_budget_free_model_proceeds(isolated_vault, capsys):
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
    
    err = capsys.readouterr().err
    assert "BUDGET WARNING: monthly_usd cap exceeded" in err
    assert "proceeding because model is FREE" in err


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
