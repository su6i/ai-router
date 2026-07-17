#!/usr/bin/env python3
"""hooks/worker_channel_nudge.py — PreToolUse hook enforcing router-only access for workers.

Blocks the FIRST direct Bash invocation matching a headless worker launch
(agy --print|-p|--prompt ... or codewhale exec ...) with a message pointing
to mcp__ai-router__delegate_agent / delegate_worker.

A deliberate second attempt passes. Interactive agy is not touched.
"""
import hashlib
import json
import sys
import tempfile
import re
from pathlib import Path

REASON = (
    "router-only workers: you are launching a headless worker directly via Bash. "
    "All headless workers (agy print, codewhale) must be routed through the router door "
    "to get ledger accounting and budget caps. Call mcp__ai-router__delegate_agent or "
    "delegate_worker instead. If this direct launch is genuinely necessary, state why "
    "in one line and retry the exact same call — the second attempt will pass."
)

def _state_dir(session_id: str) -> Path:
    d = Path(tempfile.gettempdir()) / f"worker-nudge-{session_id or 'nosession'}"
    d.mkdir(parents=True, exist_ok=True)
    return d

def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # fail-open

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    
    if tool_name not in ("Bash", "Command", "bash"):
        return

    command = tool_input.get("command", "").strip()
    if not command:
        return

    # Check for agy headless (has --print, -p, or --prompt)
    is_agy_headless = False
    if command.startswith("agy ") or command == "agy":
        if re.search(r'(?:^|\s)(--print|-p|--prompt)(?:\s|$)', command):
            is_agy_headless = True
            
    is_codewhale_exec = False
    if command.startswith("codewhale ") or command == "codewhale":
        if re.search(r'\bexec\b', command):
            is_codewhale_exec = True

    if not (is_agy_headless or is_codewhale_exec):
        return

    session_id = payload.get("session_id", "")
    
    marker = _state_dir(session_id) / hashlib.sha256(command.encode()).hexdigest()
    if marker.exists():
        return  # second attempt passes
        
    marker.touch()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }
    }))

if __name__ == "__main__":
    main()
