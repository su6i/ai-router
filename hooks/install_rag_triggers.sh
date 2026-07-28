#!/bin/bash
set -euo pipefail

# Hardcoded owner's permanent checkout path as required
DEFAULT_REPO="/Users/su6i/@-github/ai-router"
AI_ROUTER_REPO="${AI_ROUTER_REPO:-$DEFAULT_REPO}"

if [ "${1:-}" = "--cron" ]; then
    PLIST_NAME="com.ai-router.rag-sweep"
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0">'
    echo '<dict>'
    echo '    <key>Label</key>'
    echo "    <string>${PLIST_NAME}</string>"
    echo '    <key>ProgramArguments</key>'
    echo '    <array>'
    echo '        <string>uv</string>'
    echo '        <string>run</string>'
    echo '        <string>--directory</string>'
    echo "        <string>${AI_ROUTER_REPO}</string>"
    echo '        <string>python</string>'
    echo '        <string>src/rag_ingest.py</string>'
    echo '        <string>--collection</string>'
    echo '        <string>all</string>'
    echo '    </array>'
    echo '    <key>StartInterval</key>'
    echo '    <integer>1800</integer>'
    echo '    <key>StandardErrorPath</key>'
    echo "    <string>/tmp/${PLIST_NAME}.err</string>"
    echo '    <key>StandardOutPath</key>'
    echo "    <string>/tmp/${PLIST_NAME}.out</string>"
    echo '</dict>'
    echo '</plist>'
    echo ""
    echo "To install the cron sweep for SESSION.md files (which git hooks cannot observe):"
    echo "1. Save the above XML to ~/Library/LaunchAgents/${PLIST_NAME}.plist"
    echo "2. Run: launchctl load ~/Library/LaunchAgents/${PLIST_NAME}.plist"
    exit 0
fi

# Git hook installation path
CONSTITUTION_REPO="$HOME/@-github/agent-constitution"
HOOK_FILE="${CONSTITUTION_REPO}/.git/hooks/post-merge"
MARKER="# rag-auto-ingest trigger (wo-ai-router-0026)"
HOOK_CMD="uv run --directory ${AI_ROUTER_REPO} python hooks/rag_ingest_hook.py --on constitution-merge >> /dev/null 2>&1 || true"

# Ensure hooks directory exists
if [ ! -d "${CONSTITUTION_REPO}/.git/hooks" ]; then
    mkdir -p "${CONSTITUTION_REPO}/.git/hooks" || true
fi

# Create file with shebang if it doesn't exist
if [ ! -f "$HOOK_FILE" ]; then
    echo "#!/bin/sh" > "$HOOK_FILE" || { echo "Failed to write to $HOOK_FILE"; exit 1; }
    chmod +x "$HOOK_FILE"
fi

if grep -qF "$MARKER" "$HOOK_FILE" 2>/dev/null; then
    echo "already installed"
    exit 0
fi

if ! [ -w "$HOOK_FILE" ]; then
    echo "Cannot write to $HOOK_FILE. Please check permissions." >&2
    exit 1
fi

cat <<EOF >> "$HOOK_FILE"

$MARKER
$HOOK_CMD
EOF

echo "Appended to $HOOK_FILE:"
echo ""
echo "$MARKER"
echo "$HOOK_CMD"

exit 0
