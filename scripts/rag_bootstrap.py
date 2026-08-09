#!/usr/bin/env python3
"""scripts/rag_bootstrap.py — create the RAG collections' schema with ZERO data.

A fresh install needs the collection SCHEMA (Postgres tables, pgvector
extension, indexes) to be reproducible from a clean clone, but the
collection DATA (rules/skills/sessions/code actually ingested from this
machine) must never leave the machine that generated it — it lives only in
the local Postgres volume and is never committed. This script creates the
four mandated collections (see docs/ARCHITECTURE.md "RAG Index &
Auto-Ingest": rules, skills, sessions, code) as empty tables by calling each
collection's own `init_db(conn)` (defined once per indexer module — this
script never redefines a CREATE TABLE itself). Idempotent: every statement
inside `init_db()` is `IF NOT EXISTS`, so running this on every install.sh
invocation, not just the first, is safe.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import code_index
import delegate as d
import rules_index
import sessions_index
import skills_index

COLLECTIONS = [
    ("rules", rules_index),
    ("skills", skills_index),
    ("sessions", sessions_index),
    ("code", code_index),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="print the plan, touch nothing (no env load, no import of psycopg, no I/O)")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — would create empty schema for:")
        for name, _module in COLLECTIONS:
            print(f"  - {name}")
        print("No data would be ingested. No Postgres connection attempted.")
        return 0

    import os
    import psycopg

    d.load_env()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("POSTGRES_DSN not set — see README.md \"Setup (Data Plane)\" step 1", file=sys.stderr)
        return 2

    try:
        conn = psycopg.connect(dsn)
    except psycopg.OperationalError as e:
        print(f"Postgres unreachable at POSTGRES_DSN — start it first (docker compose up -d db): {e}", file=sys.stderr)
        return 2

    created = []
    try:
        for name, module in COLLECTIONS:
            module.init_db(conn)
            created.append(name)
    finally:
        conn.close()

    print(f"bootstrap ok — {len(created)} empty collections ready: {', '.join(created)}")
    print("Run `uv run src/rag_ingest.py --collection all` next to populate them from "
          "THIS machine's own rules/skills/sessions/code — that data stays local, "
          "never committed (see .gitignore).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
