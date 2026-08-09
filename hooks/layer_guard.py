#!/usr/bin/env python3
"""hooks/layer_guard.py — PreToolUse hook enforcing rule 085 §Three-Layer Delegation.

Denies `mcp__ai-router__delegate_worker` / `delegate_agent` when the caller is a
top-level premium session (manager or architect). Rule 085 fixes the shape:

    layer 3 architect  ->  layer 2 Sonnet dispatcher/reviewer  ->  layer 1 agy ($0)

The architect may drop *itself* (the manager can write the WO), but it may never
drop layer 2 and talk to the worker directly: doing so pulls the worker's output
into the premium context, which is the exact quota burn the three layers exist to
prevent, and it removes the neutral reviewer that catches worker overclaim.

Escape hatch — layer 2 only. A Sonnet subagent dispatching a WO sets
`SU6I_LAYER2=1` in its environment (or the payload carries a subagent marker) and
passes through. There is no second-attempt bypass here, unlike delegate_nudge.py:
the layer boundary is a topology invariant, not a judgement call.

Registered in ~/.claude/settings.json:
    PreToolUse  matcher "mcp__ai-router__delegate_worker|mcp__ai-router__delegate_agent"
                ->  python3 <this file>

Contract (Claude Code hooks):
    stdin  : JSON {session_id, tool_name, tool_input, ...}
    stdout : {"hookSpecificOutput": {"hookEventName": "PreToolUse",
              "permissionDecision": "deny", "permissionDecisionReason": ...}}
    exit 0 always; empty stdout = allow. Fail-open on any error — a broken hook
    must never block real work.

Every decision is appended to $VAULT/_memory/logs/layer-guard.jsonl, including the
raw payload keys. That log is how we confirm empirically which field Claude Code
uses to mark a subagent call, so the marker list below can stop being a guess.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / ".local/share/agent-projects/_memory/logs/layer-guard.jsonl"

GUARDED_TOOLS = {
    "mcp__ai-router__delegate_worker",
    "mcp__ai-router__delegate_agent",
}

# Payload fields that, when present and truthy, mean "this call comes from a
# subagent, not the top-level session". Confirmed entries stay; unconfirmed ones
# are carried here so the log can prove or drop them.
SUBAGENT_MARKER_KEYS = ("subagent_type", "agent_type", "parent_session_id", "agent_id")

REASON = (
    "rule 085 §Three-Layer Delegation: a top-level premium session may not call "
    "{tool} directly. The chain is architect -> Sonnet dispatcher/reviewer -> agy "
    "($0) worker. You may drop the architect layer, never layer 2.\n\n"
    "Do this instead: write/point to the WO file, then spawn the dispatcher —\n"
    "  Agent(subagent_type=\"general-purpose\", model=\"sonnet\",\n"
    "        prompt=\"Dispatch WO <path> to agy via mcp__ai-router__delegate_worker, "
    "review the result architect-style (tests from a neutral CWD, DoD checked live, "
    "executor claims distrusted), repair+amend on the branch, hand up only with "
    "green tests. Return branch+SHA+test output only — no diffs.\")\n\n"
    "Why the hook and not a reminder: this rule was written 2026-07-28 and broken "
    "in every session since, because ~/.claude/CLAUDE.md told the opposite. Prose "
    "lost; the deny stays.\n\n"
    "Layer 2 itself is exempt: the dispatcher runs with SU6I_LAYER2=1 set."
)


def _log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _is_layer2(payload: dict) -> bool:
    if os.environ.get("SU6I_LAYER2") == "1":
        return True
    return any(payload.get(key) for key in SUBAGENT_MARKER_KEYS)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # fail-open

    tool = payload.get("tool_name") or ""
    if tool not in GUARDED_TOOLS:
        return

    allowed = _is_layer2(payload)
    _log(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "decision": "allow" if allowed else "deny",
            "env_layer2": os.environ.get("SU6I_LAYER2") == "1",
            "payload_keys": sorted(payload.keys()),
        }
    )
    if allowed:
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": REASON.format(tool=tool),
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open
    sys.exit(0)
