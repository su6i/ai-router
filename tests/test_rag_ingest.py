import sys
import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import rag_ingest
import delegate as d
import rules_index

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import has_pg, requires_rules_corpus

def test_pg_connection_error_stderr(monkeypatch, capsys):
    import psycopg
    def mock_ingest(force=False):
        raise psycopg.OperationalError("connection failed")
        
    monkeypatch.setattr(rules_index, "ingest", mock_ingest)
    
    class Args:
        collection = "rules"
        force = False
        json_out = False
        status = False
        
    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())
    
    with pytest.raises(SystemExit) as exc:
        rag_ingest.main()
        
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Postgres unavailable — start it first: colima start" in captured.err
    assert "Traceback" not in captured.err

@pytest.mark.skipif(not has_pg, reason="Missing Postgres")
@requires_rules_corpus
def test_incremental_real_db(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    
    class Args:
        collection = "rules"
        force = False
        json_out = True
        status = False
        
    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())
    
    # First run
    rag_ingest.main()
    captured1 = capsys.readouterr()
    # The output might have multiple prints if other things print, so get the last json line
    out1 = [line for line in captured1.out.splitlines() if line.startswith("{")]
    res1 = json.loads(out1[-1])
    assert res1["status"] == "ok"
    
    # Second run
    rag_ingest.main()
    captured2 = capsys.readouterr()
    out2 = [line for line in captured2.out.splitlines() if line.startswith("{")]
    res2 = json.loads(out2[-1])
    assert res2["status"] == "ok"
    assert res2["chunks_written"] == 0
    assert res2["skipped"] > 0

def test_rag_freshness_staleness(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    state_file = tmp_path / "rag_state.json"
    
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(hours=25)).isoformat()
    recent_time = (now - timedelta(minutes=30)).isoformat()
    
    state = {
        "rules": {"last_ingest": old_time, "status": "ok"},
        "sessions": {"last_ingest": recent_time, "status": "ok"},
        "code": {"last_ingest": recent_time, "status": "failed"}
    }
    state_file.write_text(json.dumps(state))
    
    fresh = rag_ingest.rag_freshness()
    assert fresh["rules"]["stale"] is True
    assert fresh["sessions"]["stale"] is False
    assert fresh["code"]["stale"] is True


# ---------------------------------------------------------------------------
# T-970: --status code_repos wiring, sweep divergence exit-status contract
# ---------------------------------------------------------------------------


def test_status_flags_diverged_repo(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        rag_ingest.code_index, "repo_status",
        lambda: {
            "healthy-repo": {"chunks": 10, "files": 5, "diverged": False},
            "broken-repo": {"chunks": 0, "files": 3, "diverged": True},
        },
    )

    class Args:
        collection = None
        force = False
        json_out = True
        status = True

    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())

    rag_ingest.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)

    assert "code_repos" in out
    assert out["code_repos"]["broken-repo"]["diverged"] is True
    assert out["code_repos"]["healthy-repo"]["diverged"] is False


def test_status_text_output_flags_diverged_repo(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        rag_ingest.code_index, "repo_status",
        lambda: {"broken-repo": {"chunks": 0, "files": 3, "diverged": True}},
    )

    class Args:
        collection = None
        force = False
        json_out = False
        status = True

    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())

    rag_ingest.main()
    captured = capsys.readouterr()
    assert "broken-repo" in captured.out
    assert "DIVERGED" in captured.out.upper()


def test_code_sweep_unhealed_divergence_exits_loud(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)

    def fake_sweep(force=False):
        return {
            "repos_seen": 2, "repos_failed": 0, "files_seen": 0,
            "chunks_written": 0, "chunks_deleted": 0, "skipped": 0,
            "failures": {}, "empty_repos": ["r1"], "empty_repos_healed": [],
            "empty_repos_unhealed": ["r1"],
        }

    monkeypatch.setattr(rag_ingest.code_index, "sweep", fake_sweep)

    class Args:
        collection = "code"
        force = False
        json_out = True
        status = False

    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())

    with pytest.raises(SystemExit) as exc:
        rag_ingest.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    out = [line for line in captured.out.splitlines() if line.startswith("{")]
    res = json.loads(out[-1])
    assert res["empty_repos_unhealed"] == ["r1"]
    assert res["status"] == "diverged"


def test_code_sweep_healed_divergence_exits_clean(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)

    def fake_sweep(force=False):
        return {
            "repos_seen": 2, "repos_failed": 0, "files_seen": 3,
            "chunks_written": 5, "chunks_deleted": 0, "skipped": 0,
            "failures": {}, "empty_repos": ["r1"], "empty_repos_healed": ["r1"],
            "empty_repos_unhealed": [],
        }

    monkeypatch.setattr(rag_ingest.code_index, "sweep", fake_sweep)

    class Args:
        collection = "code"
        force = False
        json_out = True
        status = False

    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())

    rag_ingest.main()  # must NOT raise SystemExit
    captured = capsys.readouterr()
    out = [line for line in captured.out.splitlines() if line.startswith("{")]
    res = json.loads(out[-1])
    assert res["empty_repos_healed"] == ["r1"]
    assert res["status"] == "ok"

def test_status_no_state_file_fail_open(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    
    class Args:
        collection = None
        force = False
        json_out = True
        status = True
        
    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: Args())
    
    rag_ingest.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    
    for col in ["rules", "sessions", "code"]:
        assert col in out
        assert out[col]["stale"] is True
