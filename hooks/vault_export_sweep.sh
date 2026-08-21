#!/bin/bash
set -uo pipefail

REPO="${AI_ROUTER_REPO:-/Users/su6i/@-github/ai-router}"
MEMORY_DIR="${AI_ROUTER_MEMORY_DIR:-/Users/su6i/.local/share/agent-projects/_memory}"
# launchd hands a job PATH=/usr/bin:/bin:/usr/sbin:/sbin and nothing else, so a
# bare `command -v uv` finds nothing there even though it resolves fine in an
# interactive shell. Without the fallbacks this sweep would log "uv not found"
# and exit every 30 minutes, forever, looking installed and doing nothing.
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [ -z "$UV_BIN" ]; then
    for candidate in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        if [ -x "$candidate" ]; then
            UV_BIN="$candidate"
            break
        fi
    done
fi
CONFIG_FILE="$HOME/.config/ai-router/vault-export.env"
LOG_FILE="$MEMORY_DIR/logs/vault-export.log"
STAMP_FILE="$MEMORY_DIR/logs/.vault-export-stamp"
REGISTRY_FILE="$MEMORY_DIR/REGISTRY-IDS.md"
PLIST_LABEL="com.su6i.vault-export"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

mkdir -p "$(dirname "$LOG_FILE")"
if [ -f "$LOG_FILE" ]; then
    size=$(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$size" -gt 1048576 ]; then
        : > "$LOG_FILE"
    fi
fi

log() {
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_FILE"
}

if [ "${1:-}" = "--install" ]; then
    cat <<EOF > "$PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${REPO}/hooks/vault_export_sweep.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>UV_BIN</key>
        <string>${UV_BIN}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>/tmp/${PLIST_LABEL}.err</string>
    <key>StandardOutPath</key>
    <string>/tmp/${PLIST_LABEL}.out</string>
    <key>StartInterval</key>
    <integer>1800</integer>
</dict>
</plist>
EOF
    if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1 || true
    fi
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
    launchctl enable "gui/$(id -u)/${PLIST_LABEL}"

    CONSTITUTION_HOOK="$HOME/@-github/agent-constitution/.git/hooks/post-merge"
    MARKER="# vault-export trigger (T-165)"
    HOOK_CMD="\"$REPO/hooks/vault_export_sweep.sh\" >> /dev/null 2>&1 || true"
    
    if [ -f "$CONSTITUTION_HOOK" ]; then
        if ! grep -qF "$MARKER" "$CONSTITUTION_HOOK" 2>/dev/null; then
            cat <<EOF >> "$CONSTITUTION_HOOK"

$MARKER
$HOOK_CMD
EOF
            echo "Appended to $CONSTITUTION_HOOK:"
            echo ""
            echo "$MARKER"
            echo "$HOOK_CMD"
        fi
    else
        echo "Hook file $CONSTITUTION_HOOK does not exist; skipping post-merge hook install."
    fi
    exit 0
fi

if [ "${1:-}" = "--uninstall" ]; then
    if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1 || true
    fi
    rm -f "$PLIST_PATH"
    exit 0
fi

if [ -z "$UV_BIN" ]; then
    log "uv not found in PATH; set UV_BIN=/abs/path/to/uv and re-run"
    exit 1
fi

if [ -f "$CONFIG_FILE" ]; then
    . "$CONFIG_FILE"
fi

if [ -z "${OBSIDIAN_VAULT:-}" ]; then
    log "OBSIDIAN_VAULT not configured; skipping"
    exit 0
fi
# `. "$CONFIG_FILE"` only sets a shell variable, not an environment variable —
# vault_export.py reads os.environ, so it must be exported explicitly or the
# subprocess below sees nothing and hard-fails with "OBSIDIAN_VAULT is not set".
export OBSIDIAN_VAULT

if ! [ -d "$OBSIDIAN_VAULT" ]; then
    exit 0
fi

if [ -f "$STAMP_FILE" ] && [ -f "$REGISTRY_FILE" ]; then
    if ! [ "$REGISTRY_FILE" -nt "$STAMP_FILE" ]; then
        log "skipped (no change)"
        exit 0
    fi
fi

output=$("$UV_BIN" run --directory "$REPO" python src/vault_export.py)
log "$output"
touch "$STAMP_FILE"
exit 0
