"""One-shot, idempotent purge of phantom `repo` rows (T-956).

`session_chunks` picked up a row with `repo = '--help'` because nothing
validated the repo identity `sessions_index.py` derived while walking the
vault tree. `sessions_index.py`/`code_index.py` now reject an invalid
identity at ingest time (see `src/repo_identity.py`), but that does not
retroactively clean rows already written. This script re-checks every
distinct `repo` value already in each of the four chunk tables against the
same validation rule and deletes only the ones that fail it, naming every
value it looked at.

Safe to re-run: a repo already purged no longer appears as a distinct
value, so a second run finds nothing left to delete for it.

Usage:
    uv run --directory <repo> python src/cleanup_invalid_repos.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, _agent_projects_root  # noqa: E402
from code_index import get_repo_roots  # noqa: E402
from repo_identity import validate_repo_name, InvalidRepoIdentity  # noqa: E402

# Every chunk table that carries a `repo` column, and the roots a value in
# it must resolve under. Sessions are scoped to the vault's agent-projects
# tree; code/skills/rules are all scoped to configured code-repo roots
# (skills/rules currently only ever populate 'ai-router', but they share
# the same directory-basename identity scheme as code, not the sessions
# tree, so they get the same root set).
_TABLE_ROOTS = {
    "session_chunks": lambda: [_agent_projects_root()],
    "code_chunks": lambda: sorted({r.parent for r in get_repo_roots()}),
    "skill_chunks": lambda: sorted({r.parent for r in get_repo_roots()}),
    "rules_chunks": lambda: sorted({r.parent for r in get_repo_roots()}),
}


def cleanup_invalid_repos(dsn: str, dry_run: bool = False) -> dict:
    """Delete rows whose `repo` fails validation, in every table in `_TABLE_ROOTS`.

    Returns a report: {table: {"kept": [repo, ...], "deleted": [repo, ...]}}.
    """
    report = {}
    with psycopg.connect(dsn) as conn:
        for table, roots_fn in _TABLE_ROOTS.items():
            roots = roots_fn()
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT repo FROM {table} ORDER BY repo")
                distinct_repos = [r[0] for r in cur.fetchall()]

            kept, invalid = [], []
            for repo in distinct_repos:
                try:
                    validate_repo_name(repo, roots=roots)
                    kept.append(repo)
                except InvalidRepoIdentity:
                    invalid.append(repo)

            deleted = []
            if invalid and not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {table} WHERE repo = ANY(%s) RETURNING repo",
                        (invalid,),
                    )
                    deleted = sorted({r[0] for r in cur.fetchall()})
                conn.commit()
            elif invalid:
                deleted = sorted(invalid)  # dry-run: report what WOULD be deleted

            report[table] = {"kept": kept, "deleted": deleted}
    return report


def _print_report(report: dict, dry_run: bool) -> None:
    verb = "Would delete" if dry_run else "Deleted"
    for table, r in report.items():
        print(f"{table}: {len(r['kept'])} kept, {len(r['deleted'])} {verb.lower()}")
        for repo in r["kept"]:
            print(f"  kept:    {repo!r}")
        for repo in r["deleted"]:
            print(f"  {verb.lower()}: {repo!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, delete nothing")
    args = parser.parse_args()

    load_env()
    import os
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        sys.exit("POSTGRES_DSN not set")

    try:
        report = cleanup_invalid_repos(dsn, dry_run=args.dry_run)
    except psycopg.OperationalError:
        sys.exit("Postgres unavailable — start it first: colima start")

    _print_report(report, args.dry_run)


if __name__ == "__main__":
    main()
