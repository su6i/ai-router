#!/bin/bash
# verify_delivery.sh <abs-repo-path> [report-file]

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <abs-repo-path> [report-file]"
    exit 1
fi

REPO="$1"
REPORT="$2"

if [ ! -d "$REPO" ]; then
    echo "FAIL repo-path: does not exist ($REPO)"
    exit 1
fi

FAIL=0

cd "$REPO"

# 1. ruff green
RUFF_DIRS=""
for d in src mcp hooks tests; do
    if [ -d "$d" ]; then
        RUFF_DIRS="$RUFF_DIRS $d"
    fi
done

if [ -n "$RUFF_DIRS" ]; then
    if uvx ruff check $RUFF_DIRS >/dev/null 2>&1; then
        echo "PASS ruff: clean"
    else
        echo "FAIL ruff: issues found"
        FAIL=1
    fi
else
    echo "PASS ruff: skipped (no directories found)"
fi

# 2. Full pytest green
if uv run --directory "$REPO" --with pytest --with httpx pytest -q >/dev/null 2>&1; then
    echo "PASS pytest: full suite passed"
else
    echo "FAIL pytest: suite failed"
    FAIL=1
fi

# 3. Working tree clean
if [ -z "$(git status --porcelain)" ]; then
    echo "PASS git-status: working tree clean"
else
    echo "FAIL git-status: working tree has uncommitted changes or untracked files"
    FAIL=1
fi

# 4. Branch discipline
HEAD_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$HEAD_BRANCH" = "main" ] || [ "$HEAD_BRANCH" = "master" ]; then
    echo "FAIL git-branch: HEAD is on $HEAD_BRANCH"
    FAIL=1
else
    MSG=$(git log -1 --pretty=%B | head -n 1)
    if echo "$MSG" | grep -qE '^(feat|fix|docs|refactor|test|chore): '; then
        echo "PASS branch-discipline: on $HEAD_BRANCH with correct commit message format"
    else
        echo "FAIL branch-discipline: commit message does not match conventional format"
        FAIL=1
    fi
fi

# 5. Stub-test detector
TEST_FILES=$(git diff --name-only HEAD~1 HEAD -- '*test*.py' 2>/dev/null || true)
if [ -n "$TEST_FILES" ]; then
    STUB_OUTPUT=$(python3 -c '
import sys, ast
fail = False
for f in sys.argv[1:]:
    try:
        with open(f) as file:
            tree = ast.parse(file.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                is_stub = True
                for stmt in node.body:
                    if isinstance(stmt, ast.Pass):
                        continue
                    if isinstance(stmt, ast.Expr):
                        if isinstance(stmt.value, ast.Constant):
                            if stmt.value.value is Ellipsis or isinstance(stmt.value.value, str):
                                continue
                    is_stub = False
                    break
                if is_stub:
                    print(f"Stub test found: {node.name} in {f}")
                    fail = True
    except Exception:
        pass
if fail:
    sys.exit(1)
' $TEST_FILES 2>&1)
    if [ $? -eq 0 ]; then
        echo "PASS stub-test-detector: no stubs found in changed test files"
    else
        echo "FAIL stub-test-detector: stub tests found ($STUB_OUTPUT)"
        FAIL=1
    fi
else
    echo "PASS stub-test-detector: no test files changed"
fi

# 6. fa-docs language
FA_DOCS=$(git diff --name-only HEAD~1 HEAD -- 'docs/fa/*' '*.fa.md' 2>/dev/null || true)
if [ -n "$FA_DOCS" ]; then
    ADDED=$(git diff HEAD~1 HEAD -- $FA_DOCS | grep '^+' | grep -v '^\+++' || true)
    if echo "$ADDED" | python3 -c '
import sys, re
has_persian = any(re.search(r"[\u0600-\u06FF]", line) for line in sys.stdin)
sys.exit(0 if has_persian else 1)
'; then
        echo "PASS fa-docs: Persian characters found"
    else
        echo "FAIL fa-docs: no Persian characters found in modified Persian docs"
        FAIL=1
    fi
else
    echo "PASS fa-docs: no Persian docs changed"
fi

# 7. Report proof lines
if [ -n "$REPORT" ]; then
    if [ ! -f "$REPORT" ]; then
        echo "FAIL proof-lines: report file does not exist ($REPORT)"
        FAIL=1
    else
        REPORT_BODY=$(cat "$REPORT")
        HAS_HASH=0
        if echo "$REPORT_BODY" | grep -qEi '\b[0-9a-f]{7,40}\b'; then HAS_HASH=1; fi
        HAS_STAT=0
        if echo "$REPORT_BODY" | grep -q 'git show --stat'; then HAS_STAT=1; fi
        HAS_PYTEST=0
        if echo "$REPORT_BODY" | grep -qE '[0-9]+ passed'; then HAS_PYTEST=1; fi
        HAS_RUFF=0
        if echo "$REPORT_BODY" | grep -qi 'ruff'; then HAS_RUFF=1; fi

        if [ $HAS_HASH -eq 1 ] && [ $HAS_STAT -eq 1 ] && [ $HAS_PYTEST -eq 1 ] && [ $HAS_RUFF -eq 1 ]; then
            echo "PASS proof-lines: all required proofs present in report"
        else
            echo "FAIL proof-lines: missing proofs (hash=$HAS_HASH stat=$HAS_STAT pytest=$HAS_PYTEST ruff=$HAS_RUFF)"
            FAIL=1
        fi
    fi
fi

# 8. Foreign-repo guard
FOREIGN_REPOS="$HOME/@-github/agent-constitution $HOME/@-github/polycast"
for frepo in $FOREIGN_REPOS; do
    if [ -d "$frepo" ]; then
        MODS=$(git -C "$frepo" status -s | grep -E '^ M|^A|^D|^R' || true)
        if [ -n "$MODS" ]; then
            echo "WARN foreign-repo: modifications found in $frepo"
            echo "$MODS" | sed 's/^/  /'
        fi
    fi
done

exit $FAIL
