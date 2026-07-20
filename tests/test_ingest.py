import pytest
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from ingest import parse_line

def test_parse_line_chat():
    line = '{"ts": "2026-07-14T12:00:00Z", "model_asked": "flash", "model_echoed": "deepseek-v4-flash", "id": "resp-123", "in": 100, "out": 50, "cache": 0, "cost_usd": 0.001, "latency_s": 1.2, "cached": false}'
    row = parse_line(line)
    assert row is not None
    assert row["mode"] == "chat"
    assert row["model_asked"] == "flash"
    assert row["model"] == "deepseek-v4-flash"
    assert row["response_id"] == "resp-123"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["cache_tokens"] == 0
    assert row["cost_usd"] == 0.001
    assert row["latency_s"] == 1.2
    assert row["cached"] is False
    assert row["raw"] == line

def test_parse_line_worker():
    line = '{"ts": "2026-07-14T12:01:00Z", "model_asked": "flash", "model_echoed": "deepseek-v4-flash", "mode": "worker", "files_written": ["foo.py"], "cost_usd": 0.002, "cached": false, "attempts": 1}'
    row = parse_line(line)
    assert row is not None
    assert row["mode"] == "worker"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cache_tokens"] is None
    assert row["cost_usd"] == 0.002
    assert row["cached"] is False

def test_parse_line_malformed():
    assert parse_line("") is None
    assert parse_line("{malformed json") is None
    assert parse_line('{"ts": "2026-07-14T12:01:00Z"}') is None # missing model_asked
    assert parse_line('{"model_asked": "flash"}') is None # missing ts

def _pg_available() -> bool:
    # A set POSTGRES_DSN is not enough — Colima/Postgres may be stopped, in
    # which case the integration test must skip, not fail. Probe a real
    # connection with a short timeout.
    try:
        import psycopg
        from delegate import load_env
        load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False
        psycopg.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")
def test_integration_ingest_idempotent(capsys):
    # Runs against the real DB and real audit.log; safe because ingest is
    # idempotent by design (ON CONFLICT DO NOTHING on the line hash).
    from ingest import ingest
    ingest()
    capsys.readouterr()
    ingest()
    out = capsys.readouterr().out
    assert "Inserted: 0." in out
