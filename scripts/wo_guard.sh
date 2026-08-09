#!/usr/bin/env bash
# wo_guard.sh — mechanical guard for delegated work orders.
#
# Prompt rules are advice a worker can ignore. This runs inside --verify, so a
# violation makes the delegation FAIL instead of being reported as PASS. Every
# check here exists because a delegated run actually got it wrong:
#
#   * a worker did its work on whatever branch happened to be checked out
#   * a retry re-applied a patch that was already in the file
#   * a run reported test counts and file counts it had never measured
#
# Usage (put it FIRST in --verify, chained with &&):
#
#   scripts/wo_guard.sh --repo <abs-path> --branch <name> --base <sha> \
#       [--once <file>:<regex>:<n>]... [--then <command>]
#
# The receipt printed at the end is the only numeric claim the architect
# accepts: it is measured here, not narrated by the model.

set -uo pipefail

REPO=""; BRANCH=""; BASE=""; THEN=""; ONCE=()

die() { printf '\n❌ WO-GUARD FAILED: %s\n' "$1" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   REPO="${2:?--repo needs a path}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch needs a name}"; shift 2 ;;
    --base)   BASE="${2:?--base needs a sha}"; shift 2 ;;
    --once)   ONCE+=("${2:?--once needs file:regex:n}"); shift 2 ;;
    --then)   shift; THEN="$*"; break ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$REPO" ]   || die "--repo is required (absolute path)"
[ -n "$BRANCH" ] || die "--branch is required"
[ -n "$BASE" ]   || die "--base is required (the sha the branch must descend from)"

cd "$REPO" || die "repo not found: $REPO"
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository: $REPO"

# 1. Right branch. Catches work done on main, on a leftover branch, or on a
#    detached HEAD — the failure that is invisible in a diff.
HEAD_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$HEAD_BRANCH" = "$BRANCH" ] || \
  die "wrong branch: HEAD is '$HEAD_BRANCH', the task requires '$BRANCH'.
    Nothing you did counts. Do NOT cherry-pick it across — stop and report."

# 2. Right base. A branch cut from a stale point silently reverts whatever
#    landed in between, and the diff looks perfectly clean.
git cat-file -e "${BASE}^{commit}" 2>/dev/null || die "base commit not found: $BASE"
git merge-base --is-ancestor "$BASE" HEAD || \
  die "'$BRANCH' does not descend from $BASE. It was cut from the wrong place;
    merging it would revert work committed since. Stop and report."

# 3. Clean merge state. An unresolved merge makes every later check meaningless.
if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
  die "unresolved merge conflicts:
$(git diff --name-only --diff-filter=U | sed 's/^/      /')"
fi
MARKERS="$(git grep -lE '^(<<<<<<<|=======|>>>>>>>) ' -- . 2>/dev/null || true)"
[ -z "$MARKERS" ] || die "conflict markers left in tracked files:
$(printf '%s\n' "$MARKERS" | sed 's/^/      /')"

# 4. Idempotence. `--once path:regex:n` asserts the pattern appears exactly n
#    times — the cheap way to catch a patch applied twice.
for spec in "${ONCE[@]:-}"; do
  [ -n "$spec" ] || continue
  file="${spec%%:*}"; rest="${spec#*:}"
  regex="${rest%:*}"; want="${rest##*:}"
  case "$want" in
    ''|*[!0-9]*) die "--once needs file:regex:<count>, got '$spec'.
    The count is missing or not a number, so nothing would have been checked." ;;
  esac
  [ -f "$file" ] || die "--once target does not exist: $file"
  got="$(grep -cE -- "$regex" "$file" || true)"
  [ "$got" = "$want" ] || \
    die "'$regex' occurs $got time(s) in $file, expected exactly $want.
    $( [ "$got" -gt "$want" ] && echo 'That is a patch applied more than once.' || echo 'The edit did not land.')"
done

# 5. ZWNJ (U+200C, نیم‌فاصله) regression. A delegated Persian-text edit has
#    silently dropped or replaced half-spaces before (fix/du-naming-and-
#    interim-track, ApplyForge, 2026-08 — restored by hand). This is a
#    worker-model text-generation loss, not a bug in this pipeline (proven
#    by tests/test_zwnj_preservation.py), so the only defence available here
#    is mechanical detection: fail the delegation instead of reporting a
#    silent PASS. See scripts/zwnj_guard.py for the full receipt format.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/zwnj_guard.py" ]; then
  ZWNJ_OUTPUT="$(python3 "$SCRIPT_DIR/zwnj_guard.py" --repo "$REPO" --base "$BASE" 2>&1)" || \
    die "ZWNJ half-spaces were lost by this delegation — see the receipt above:
$(printf '%s\n' "$ZWNJ_OUTPUT" | sed 's/^/    /')"
  printf '%s\n' "$ZWNJ_OUTPUT"
fi

# 6. Receipt. Measured facts only. Anything the model says that contradicts
#    this block is a fabrication, and the architect trusts this block.
FILES_CHANGED="$(git diff --name-only "$BASE"..HEAD | wc -l | tr -d ' ')"
UNCOMMITTED="$(git status --porcelain | wc -l | tr -d ' ')"
COMMITS="$(git rev-list --count "$BASE"..HEAD)"

emit_receipt() {
  printf '\n===WO-GUARD-RECEIPT===\n'
  printf 'repo=%s\n' "$REPO"
  printf 'branch=%s\n' "$HEAD_BRANCH"
  printf 'base=%s\n' "$(git rev-parse --short "$BASE")"
  printf 'head=%s\n' "$(git rev-parse --short HEAD)"
  printf 'commits_on_top_of_base=%s\n' "$COMMITS"
  printf 'files_changed_vs_base=%s\n' "$FILES_CHANGED"
  printf 'uncommitted_paths=%s\n' "$UNCOMMITTED"
  printf 'verify_exit=%s\n' "$1"
  printf '===END-WO-GUARD-RECEIPT===\n'
}

if [ -z "$THEN" ]; then
  emit_receipt 0
  exit 0
fi

printf '✅ wo-guard: on %s, descended from %s. Running verify.\n\n' "$BRANCH" "$(git rev-parse --short "$BASE")"
bash -c "$THEN"
rc=$?
emit_receipt "$rc"
exit "$rc"
