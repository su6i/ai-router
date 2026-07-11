#!/usr/bin/env python3
"""hooks/delegate_nudge.py — PreToolUse hook enforcing delegation-first routing.

Blocks the FIRST large code Write/Edit by the premium architect model and
points it at mcp__ai-router__delegate_worker instead. A second attempt on the
same file in the same session passes — that is the deliberate escape hatch
for genuinely architecture-critical code (the architect must consciously
retry, with a one-line justification in its reasoning).

Registered globally in ~/.claude/settings.json:
    PreToolUse  matcher "Write|Edit"  ->  python3 <this file>

Contract (Claude Code hooks):
    stdin  : JSON {session_id, tool_name, tool_input, ...}
    stdout : {"hookSpecificOutput": {"hookEventName": "PreToolUse",
              "permissionDecision": "deny", "permissionDecisionReason": ...}}
    exit 0 always; empty stdout = allow. Fail-open on any error: a broken
    hook must never block real work.
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

THRESHOLD_LINES = 40

CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb",
    ".php", ".sh", ".zsh", ".bash", ".sql", ".css", ".scss", ".vue",
    ".svelte", ".pl", ".lua", ".r",
}

REASON = (
    "delegation-first (Cost Routing): this is a {n}-line code write to {path} — "
    "grunt implementation belongs to the cheap worker, not the premium architect. "
    "Call mcp__ai-router__delegate_worker instead: pass prompt + files (paths, "
    "not contents) + allow_write + workdir + verify. Ladder: gemini (free) -> "
    "flash/pro. If this code is genuinely architecture-critical and only the "
    "architect may write it, state why in one line and retry the exact same "
    "call — the second attempt on this file will pass."
)


def _state_dir(session_id: str) -> Path:
    d = Path(tempfile.gettempdir()) / f"delegate-nudge-{session_id or 'nosession'}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_code_lines(tool_name: str, tool_input: dict) -> int:
    if tool_name == "Write":
        return len((tool_input.get("content") or "").splitlines())
    if tool_name == "Edit":
        return len((tool_input.get("new_string") or "").splitlines())
    return 0


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # fail-open

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if tool_name not in ("Write", "Edit") or not file_path:
        return

    p = Path(file_path)
    if p.suffix.lower() not in CODE_SUFFIXES:
        return  # docs/config/markdown: the architect writes those directly
    lower = file_path.lower()
    if "/scratchpad" in lower or lower.startswith(tempfile.gettempdir().lower()):
        return  # throwaway scratch files are never worth a delegation round-trip

    n = _new_code_lines(tool_name, tool_input)
    if n <= THRESHOLD_LINES:
        return

    marker = _state_dir(payload.get("session_id", "")) / hashlib.sha256(
        file_path.encode()).hexdigest()
    if marker.exists():
        return  # second attempt on this file: architect insisted — allow
    marker.touch()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON.format(n=n, path=file_path),
        }
    }))


if __name__ == "__main__":
    main()
