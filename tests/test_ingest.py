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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import requires_pg  # noqa: E402  (sits below the sys.path setup it needs)


@requires_pg
def test_integration_ingest_idempotent(capsys):
    # AUDIT is pointed at a per-run temp file by the session-scoped
    # `isolate_vault` fixture in conftest.py (which patches `ingest.AUDIT`
    # directly, since this module's own `from delegate import AUDIT` made a
    # separate binding). Seed it with fixture rows so the test still
    # exercises real insert-then-idempotent-noop behaviour instead of
    # depending on whatever the developer's own audit.log happened to
    # contain (T-943).
    from ingest import AUDIT, ingest

    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(
        '{"ts": "2026-07-14T12:00:00Z", "model_asked": "flash", '
        '"model_echoed": "deepseek-v4-flash", "id": "resp-ingest-test-1", '
        '"in": 10, "out": 5, "cache": 0, "cost_usd": 0.0001, '
        '"latency_s": 0.5, "cached": false}\n'
        '{"ts": "2026-07-14T12:01:00Z", "model_asked": "flash", '
        '"model_echoed": "deepseek-v4-flash", "id": "resp-ingest-test-2", '
        '"in": 20, "out": 10, "cache": 0, "cost_usd": 0.0002, '
        '"latency_s": 0.6, "cached": false}\n'
    )

    ingest()
    capsys.readouterr()
    ingest()
    out = capsys.readouterr().out
    assert "Inserted: 0." in out
