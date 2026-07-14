#!/usr/bin/env python3
"""Ingest audit.log into Postgres."""

import hashlib
import json
import os
import sys
import psycopg

from delegate import load_env, AUDIT

def parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None

    event_id = hashlib.sha256(line.encode("utf-8")).hexdigest()
    
    ts = rec.get("ts")
    model_asked = rec.get("model_asked")
    if not ts or not model_asked:
        return None

    mode = rec.get("mode", "chat")

    return {
        "event_id": event_id,
        "response_id": rec.get("id"),
        "ts": ts,
        "project": rec.get("project"),
        "commit_sha": rec.get("commit"),
        "session_id": rec.get("session"),
        "model_asked": model_asked,
        "model": rec.get("model_echoed"),
        "mode": mode,
        "via": rec.get("via"),
        "cached": rec.get("cached", False),
        "input_tokens": rec.get("in") if mode == "chat" else None,
        "output_tokens": rec.get("out") if mode == "chat" else None,
        "cache_tokens": rec.get("cache") if mode == "chat" else None,
        "cost_usd": rec.get("cost_usd", 0.0),
        "latency_s": rec.get("latency_s"),
        "raw": line
    }

def ingest():
    load_env()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("POSTGRES_DSN not set in vault .env", file=sys.stderr)
        sys.exit(1)

    if not AUDIT.exists():
        print(f"No audit log found at {AUDIT}")
        return

    malformed = 0
    inserted = 0

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            with AUDIT.open("r") as f:
                for line in f:
                    row = parse_line(line)
                    if row is None:
                        if line.strip():
                            malformed += 1
                        continue

                    try:
                        cur.execute("""
                            INSERT INTO usage (
                                event_id, response_id, ts, project, commit_sha, session_id,
                                model_asked, model, mode, via, cached, input_tokens,
                                output_tokens, cache_tokens, cost_usd, latency_s, raw
                            ) VALUES (
                                %(event_id)s, %(response_id)s, %(ts)s, %(project)s, %(commit_sha)s,
                                %(session_id)s, %(model_asked)s, %(model)s, %(mode)s, %(via)s,
                                %(cached)s, %(input_tokens)s, %(output_tokens)s, %(cache_tokens)s,
                                %(cost_usd)s, %(latency_s)s, %(raw)s
                            ) ON CONFLICT (event_id) DO NOTHING
                        """, row)
                        if cur.rowcount > 0:
                            inserted += 1
                    except Exception as e:
                        print(f"Failed to insert row {row['event_id']}: {e}", file=sys.stderr)
                        malformed += 1
            conn.commit()

    print(f"Ingest complete. Inserted: {inserted}. Skipped malformed/errors: {malformed}")

if __name__ == "__main__":
    ingest()
