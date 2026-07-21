#!/usr/bin/env bash
# session_start_inbox.sh — checks for unread messages via the router inbox
# 
# To use, add this script to your global claude settings:
# ~/.claude/settings.json
# {
#   "SessionStart": "/path/to/ai-router/hooks/session_start_inbox.sh"
# }

# Source the router wrapper if we're outside of it
if ! command -v r >/dev/null 2>&1; then
    # Adjust this path if your ai-router checkout is somewhere else
    AI_ROUTER_REPO="${AI_ROUTER_REPO:-$HOME/@-github/ai-router}"
    if [ -f "$AI_ROUTER_REPO/shell/r.sh" ]; then
        source "$AI_ROUTER_REPO/shell/r.sh"
    else
        echo "ai-router shell wrapper not found at $AI_ROUTER_REPO/shell/r.sh" >&2
        exit 1
    fi
fi

# Print the unread note count (if any) and exit.
# This runs `r inbox --peek` which just shows counts without marking read.
r inbox --peek
