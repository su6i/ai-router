#!/bin/bash
set -euo pipefail

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--dry-run" ]; }; then
    echo "Usage: $0 [--dry-run]" >&2
    exit 1
fi

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="${AI_ROUTER_ENV_FILE:-$HOME/.local/share/agent-projects/ai-router/secrets/.env}"
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found. See README.md \"Setup (Data Plane)\" step 1" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    if command -v colima >/dev/null 2>&1; then
        echo "starting colima..."
        if [ "$DRY_RUN" = false ]; then
            colima start
        fi
    else
        echo "Error: start Docker Desktop or colima first" >&2
        exit 1
    fi
fi

echo "docker compose up -d db"
if [ "$DRY_RUN" = false ]; then
    (cd "$REPO_ROOT" && docker compose up -d db)
fi

if [ "$DRY_RUN" = false ]; then
    for i in {1..30}; do
        if [ "$(docker inspect --format='{{.State.Health.Status}}' ai-router-db 2>/dev/null || true)" = "healthy" ]; then
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "Warning: db health check timed out" >&2
        fi
        sleep 2
    done
else
    echo "waiting for db health (skipped in dry-run)..."
fi

BOOTSTRAP_CMD="uv run --directory $REPO_ROOT python scripts/rag_bootstrap.py"
if [ "$DRY_RUN" = true ]; then
    BOOTSTRAP_CMD="$BOOTSTRAP_CMD --dry-run"
fi

echo "$BOOTSTRAP_CMD"
if [ "$DRY_RUN" = true ]; then
    uv run --directory "$REPO_ROOT" python scripts/rag_bootstrap.py --dry-run
else
    uv run --directory "$REPO_ROOT" python scripts/rag_bootstrap.py
fi

echo "Install complete!"
