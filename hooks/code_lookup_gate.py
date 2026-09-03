#!/usr/bin/env python3
"""hooks/code_lookup_gate.py — PreToolUse hook for Read tool to encourage code_lookup.

Blocks exploratory Read calls on large files (>8KB by default) unless the agent
has recently called mcp__ai-router__code_lookup, pointing the agent to use
the semantic search instead of dumping whole files into context. Exempts files
the agent has recently written/edited in this session — "recently" here means
"within the scanned transcript tail" (see TRANSCRIPT_TAIL_BYTES below), not
"at any point in the session", since only the tail is ever scanned.

Known limitation: this hook has no way to check whether the code_lookup MCP
server is actually reachable. If it is down, the gate still blocks the first
Read of a large file exactly as if code_lookup were healthy; the deliberate
second attempt on the same path still passes, same as any other block.

A deliberate second attempt passes.

Registered in ~/.claude/settings.json:
    PreToolUse  matcher "Read"  ->  python3 <this file>
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# PreToolUse runs synchronously in front of every Read call. A long session's
# transcript can be tens of MB; parsing it top-to-bottom on every large Read
# would add real, user-visible latency to routine tool calls. Recency only
# needs recent history anyway, so only the last TRANSCRIPT_TAIL_BYTES bytes of
# the transcript are ever read (seeking from the end, not reading-then-slicing
# the whole file). Override with AI_ROUTER_CODE_LOOKUP_GATE_TRANSCRIPT_TAIL_BYTES
# if a session's tool-call density ever needs a bigger or smaller window.
TRANSCRIPT_TAIL_BYTES = 1_000_000

def _state_dir(session_id: str) -> Path:
    d = Path(tempfile.gettempdir()) / f"code-lookup-gate-{session_id or 'nosession'}"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _find_repo_name(file_path: str):
    """Walk up from file_path's directory to the nearest git root and return
    its basename, or None if no git root is found. This hook is registered
    globally (~/.claude/settings.json) and fires in whichever repo the owner
    is working in, so the repo must be derived per-call from the target file
    rather than hardcoded — a hardcoded repo would send an agent working in
    one repo off to search a different repo's code_lookup index.

    ".git" can be a directory (a normal checkout) or a file (a git worktree,
    whose ".git" is a pointer file to the real gitdir) — both count.
    """
    d = os.path.dirname(os.path.abspath(file_path))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent

def _read_transcript_tail(transcript_path: str, tail_bytes: int) -> str:
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as f:
        if size > tail_bytes:
            f.seek(-tail_bytes, os.SEEK_END)
            f.readline()  # discard the leading partial line from the seek
        data = f.read()
    return data.decode("utf-8", errors="replace")

def _parse_transcript(transcript_path: str, target_file_path: str, recent_n: int,
                       tail_bytes: int):
    if not transcript_path:
        raise ValueError("No transcript path provided")

    tool_uses = []
    is_recently_written = False
    total_lines = 0
    parsed_lines = 0

    for line in _read_transcript_tail(transcript_path, tail_bytes).splitlines():
        line = line.strip()
        if not line:
            continue
        total_lines += 1
        try:
            obj = json.loads(line)
        except Exception:
            continue
        parsed_lines += 1

        if obj.get("type") == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    tool_uses.append(block)
                    name = block.get("name")
                    if name in ("Write", "Edit"):
                        inp = block.get("input", {})
                        if inp.get("file_path") == target_file_path:
                            is_recently_written = True

    if total_lines > 0 and parsed_lines == 0:
        # Every non-blank line in the scanned tail failed to parse as JSON:
        # the transcript (or the tail we read of it) is corrupt, not merely a
        # session with no recorded tool calls yet. We cannot verify anything
        # from it, so let the caller fail open instead of treating
        # "unparseable" the same as "nothing found".
        raise ValueError(f"transcript unparseable: {transcript_path}")

    has_recent_code_lookup = False
    recent_tools = tool_uses[-recent_n:] if recent_n > 0 else []
    for block in recent_tools:
        if block.get("name") == "mcp__ai-router__code_lookup":
            has_recent_code_lookup = True
            break

    return is_recently_written, has_recent_code_lookup

def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    if payload.get("tool_name") != "Read":
        return

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return

    try:
        size = os.path.getsize(file_path)
    except Exception:
        return

    try:
        threshold = int(os.environ.get("AI_ROUTER_CODE_LOOKUP_GATE_MAX_BYTES", "8192"))
    except Exception:
        threshold = 8192

    if size <= threshold:
        return

    try:
        recent_n = int(os.environ.get("AI_ROUTER_CODE_LOOKUP_GATE_RECENT_N", "10"))
    except Exception:
        recent_n = 10

    try:
        tail_bytes = int(os.environ.get(
            "AI_ROUTER_CODE_LOOKUP_GATE_TRANSCRIPT_TAIL_BYTES", TRANSCRIPT_TAIL_BYTES))
    except Exception:
        tail_bytes = TRANSCRIPT_TAIL_BYTES

    transcript_path = payload.get("transcript_path") or ""

    try:
        is_recently_written, has_recent_code_lookup = _parse_transcript(
            transcript_path, file_path, recent_n, tail_bytes)
    except Exception:
        return

    if is_recently_written:
        return

    if has_recent_code_lookup:
        return

    session_id = payload.get("session_id", "")
    marker = _state_dir(session_id) / hashlib.sha256(file_path.encode()).hexdigest()
    if marker.exists():
        return

    marker.touch()

    path_hint = os.path.dirname(file_path) or "."
    repo_name = _find_repo_name(file_path)
    repo_arg = f', repo="{repo_name}"' if repo_name else ""
    call_hint = (
        f'mcp__ai-router__code_lookup(query="<what you\'re looking for>", '
        f'path_prefix="{path_hint}"{repo_arg})'
    )
    reason = (
        f"code_lookup gate: {file_path} is {size} bytes (over the {threshold}-byte threshold) "
        f"and mcp__ai-router__code_lookup has not been called in the last {recent_n} tool calls this session. "
        f"Call {call_hint} first "
        "— it returns only the relevant chunks instead of the whole file. If you deliberately need the raw file, "
        f"retry this exact Read — the second attempt on {file_path} will pass."
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))

if __name__ == "__main__":
    main()
