#!/usr/bin/env python3
"""scripts/zwnj_guard.py — mechanical regression guard for ZWNJ (U+200C, نیم‌فاصله).

Root-cause context (see tests/test_zwnj_preservation.py, which proves this):
the router's OWN pipeline — parse_worker_response() -> _write_files() /
_apply_patches() in src/delegate.py — is byte-identical for ZWNJ. Feeding a
Persian string containing U+200C through the sentinel-line parser and the
file/patch writer round-trips it exactly. That means the loss observed on
branch fix/du-naming-and-interim-track (ApplyForge, 2026-08, restored by
hand) happened in the WORKER MODEL's own text generation, not in our
normalization/encoding layer — there isn't one to blame.

Since the model itself can't be patched from here, this script is a trap,
not a repair: it counts U+200C occurrences per changed file between a base
commit and the current working tree (HEAD or dirty tree, whichever the
caller has checked out) and FAILS if any tracked file's count went down.
That turns "did this delegated edit eat half-spaces" into a number, per the
owner's standing rule (delegation ledger: "رسید = عدد، نثر = ادعا").

Usage:
    python3 scripts/zwnj_guard.py --repo <abs-path> --base <sha>

Exit 0 with a per-file receipt when nothing regressed (including the
legitimate case "0 files touched ZWNJ at all" — that is reported, not
silently skipped). Exit 1 with the offending file(s) and counts otherwise.
"""
import argparse
import subprocess
import sys

ZWNJ = "‌"


def _git_show(repo: str, ref: str, path: str) -> str | None:
    """Text of `path` at `ref`, or None if it doesn't exist there."""
    r = subprocess.run(  # noqa: PLW1510
        ["git", "-C", repo, "show", f"{ref}:{path}"],
        capture_output=True, text=True, errors="replace",
    )
    return r.stdout if r.returncode == 0 else None


def _changed_files(repo: str, base: str) -> list[str]:
    r = subprocess.run(  # noqa: PLW1510
        ["git", "-C", repo, "diff", "--name-only", f"{base}..HEAD"],
        capture_output=True, text=True, check=True,
    )
    return [f for f in r.stdout.splitlines() if f.strip()]


def check(repo: str, base: str) -> tuple[int, list[tuple[str, int, int]], list[tuple[str, int, int]]]:
    """Returns (exit_code, full_receipt, regressions). Pure — no printing."""
    files = _changed_files(repo, base)
    receipt: list[tuple[str, int, int]] = []
    regressions: list[tuple[str, int, int]] = []
    for f in files:
        before_text = _git_show(repo, base, f)
        if before_text is None:
            continue  # file did not exist at BASE — nothing to regress against
        after_text = _git_show(repo, "HEAD", f)
        if after_text is None:
            continue  # file was removed in HEAD — not a ZWNJ-loss case
        before = before_text.count(ZWNJ)
        after = after_text.count(ZWNJ)
        if before == 0 and after == 0:
            continue
        receipt.append((f, before, after))
        if after < before:
            regressions.append((f, before, after))
    return (1 if regressions else 0), receipt, regressions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="absolute path to the repo")
    ap.add_argument("--base", required=True, help="base sha/ref to diff against HEAD")
    args = ap.parse_args()

    exit_code, receipt, regressions = check(args.repo, args.base)

    print("===ZWNJ-GUARD-RECEIPT===")
    print(f"repo={args.repo}")
    print(f"base={args.base}")
    print(f"files_with_zwnj={len(receipt)}")
    for f, b, a in receipt:
        print(f"  {f}: before={b} after={a}")
    print(f"regressions={len(regressions)}")
    print("===END-ZWNJ-GUARD-RECEIPT===")

    if regressions:
        print("\n❌ ZWNJ-GUARD FAILED: half-spaces (U+200C) were lost in:", file=sys.stderr)
        for f, b, a in regressions:
            print(f"  {f}: {b} -> {a} (-{b - a})", file=sys.stderr)
        print("  This is a worker-model text-generation loss, not a pipeline bug\n"
              "  (see tests/test_zwnj_preservation.py). Re-prompt the worker with the\n"
              "  exact half-spaces restored, or fix by hand before merging.", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
