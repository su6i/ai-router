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
    PreToolUse  matcher "Read|Grep|Glob"  ->  python3 <this file>
    PreToolUse  matcher "Bash"            ->  python3 <this file>  (command-shape gating added separately)
"""
import hashlib
import json
import os
import shlex
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

def _log_decision(repo, tool, target, decision) -> None:
    """Append one JSON line to the gate's decision log so its real hit/miss
    rate is measurable instead of assumed. Best-effort only: a logging
    failure must never crash or block the hook."""
    try:
        log_path = os.environ.get("AI_ROUTER_LOOKUP_GATE_LOG") or str(
            Path(tempfile.gettempdir()) / "code-lookup-gate.log")
        with open(log_path, "a") as f:
            f.write(json.dumps({"repo": repo, "tool": tool, "target": target, "decision": decision}) + "\n")
    except Exception:
        pass

def _repo_has_chunks(repo_name) -> bool:
    """True only if `code_chunks` provably has at least one row for repo_name.
    Fails toward False (treat as empty / do not block) on ANY problem —
    missing repo_name, missing POSTGRES_DSN, connection error, timeout, missing
    table. Rationale: blocking an agent from reading a file when we cannot
    PROVE the index has data for that repo is worse than letting the read
    through (T-953: every repo but ai-router has zero code_chunks rows today).

    This is the ONLY place in this file that opens a database connection, and
    it must be called ONLY immediately before a block would otherwise be
    issued — every cheap/free check (kill switch, tool_name, size threshold,
    recent code_lookup, recently-written, one-time marker) must already have
    run and decided to block before this function is ever invoked. Do not
    move this check earlier in the flow; a DB round-trip on every gated call
    would add real latency to routine tool use.
    """
    if not repo_name:
        return True
    try:
        import psycopg
        src_dir = str(Path(__file__).resolve().parent.parent / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from delegate import load_env
        load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return False
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM code_chunks WHERE repo = %s LIMIT 1", (repo_name,))
                return cur.fetchone() is not None
    except Exception:
        return False

GATED_BASH_UTILS = {"cat", "head", "tail", "sed", "grep", "rg", "ag", "find"}

def _bash_gate_target(command: str):
    """Return the filesystem path this Bash command appears to read/search
    directly, or None if the command should NOT be gated.

    Deliberately conservative (false negatives over false positives): this
    only looks at the FIRST pipeline segment's leading utility, so anything
    consuming another command's output -- `history | grep foo`,
    `git diff | grep foo`, any pipeline whose source is not the filesystem --
    is never gated (grep/rg/ag/etc only trigger this when they are the FIRST
    command in the line, i.e. reading straight off disk, not off a pipe).
    A command containing a heredoc or here-string (`<<`, `<<<`) is never
    gated, since the leading utility would be reading inline text handed to
    it by the shell, not a file on disk. `git log --grep=...` is excluded
    for free: "git" is never a gated leading utility, regardless of the
    flags that follow it -- we never look past the first word to decide
    whether a later flag happens to spell "grep".

    Known limitation, accepted for this iteration: shell constructs this
    simple tokenizer does not understand (process substitution `<(...)`,
    command substitution, quoted pipes) are not specially handled. Given the
    choice between a hand-rolled full shell grammar and staying conservative
    on the documented cases, this stays conservative and simple.
    """
    if "<<" in command:
        return None

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quotes etc. -- cannot parse safely, do not gate.
        return None

    if not tokens:
        return None

    # Only the first pipeline segment matters -- see docstring.
    first_segment = []
    for tok in tokens:
        if tok in ("|", "&&", "||", ";", "&"):
            break
        first_segment.append(tok)

    if not first_segment:
        return None

    utility = os.path.basename(first_segment[0])
    if utility not in GATED_BASH_UTILS:
        return None

    args = first_segment[1:]

    if utility == "find":
        if not any(a in ("-name", "-iname") for a in args):
            return None
        positional = [a for a in args if not a.startswith("-")]
        return positional[0] if positional else None

    if utility == "sed" and not any(a == "-n" or a.startswith("-n") for a in args):
        return None

    # Generic case (cat/head/tail/grep/rg/ag/sed -n ...): the last token
    # that doesn't look like a flag is the target. No such token means the
    # command is reading stdin (e.g. bare `cat`), not a file -- don't gate.
    positional = [a for a in args if not a.startswith("-")]
    return positional[-1] if positional else None

def _handle_bash(payload) -> None:
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command:
        return

    target = _bash_gate_target(command)
    if not target:
        return

    cwd = payload.get("cwd") or "."
    abs_target = target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd, target))

    # DoD: only gate reads/searches aimed at files inside a git repo.
    # The "probe" trick (see _find_repo_name's own docstring for why this
    # walks from a directory) works whether abs_target is itself a file or
    # a directory: appending a fake filename before taking dirname() always
    # lands on abs_target's own directory, so a directory that IS a git
    # root (e.g. `find /repo -name '*.py'` where /repo/.git exists) is still
    # detected correctly instead of walking one level too far up.
    repo_name = _find_repo_name(os.path.join(abs_target, "probe"))
    if repo_name is None:
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
            transcript_path, abs_target, recent_n, tail_bytes)
    except Exception:
        return

    if is_recently_written:
        return

    if has_recent_code_lookup:
        return

    if not _repo_has_chunks(repo_name):
        _log_decision(repo_name, "Bash", command, "pass-empty-index")
        return

    session_id = payload.get("session_id", "")
    marker = _state_dir(session_id) / hashlib.sha256(("Bash:" + command).encode()).hexdigest()
    if marker.exists():
        _log_decision(repo_name, "Bash", command, "pass-second-attempt")
        return

    marker.touch()

    repo_arg = f', repo="{repo_name}"' if repo_name else ""
    path_hint = os.path.dirname(abs_target) or abs_target
    call_hint = (
        f'mcp__ai-router__code_lookup(query="<what you\'re looking for>", '
        f'path_prefix="{path_hint}"{repo_arg})'
    )
    reason = (
        f"code_lookup gate: this Bash command reads/searches {abs_target} directly "
        f"and mcp__ai-router__code_lookup has not been called in the last {recent_n} tool calls this session. "
        f"Call {call_hint} first "
        "— it returns only the relevant chunks instead of a raw file scan. If you deliberately need the raw "
        "command, retry this exact Bash command — the second attempt will pass."
    )

    _log_decision(repo_name, "Bash", command, "block")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))

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
    if os.environ.get("AI_ROUTER_LOOKUP_GATE", "").strip().lower() == "off":
        return

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    tool_name = payload.get("tool_name")
    if tool_name == "Read":
        _handle_read(payload)
    elif tool_name in ("Grep", "Glob"):
        _handle_grep_or_glob(payload, tool_name)
    elif tool_name == "Bash":
        _handle_bash(payload)

def _handle_read(payload) -> None:
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

    repo_name = _find_repo_name(file_path)
    if not _repo_has_chunks(repo_name):
        _log_decision(repo_name, "Read", file_path, "pass-empty-index")
        return

    session_id = payload.get("session_id", "")
    marker = _state_dir(session_id) / hashlib.sha256(file_path.encode()).hexdigest()
    if marker.exists():
        _log_decision(repo_name, "Read", file_path, "pass-second-attempt")
        return

    marker.touch()

    path_hint = os.path.dirname(file_path) or "."
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

    _log_decision(repo_name, "Read", file_path, "block")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))

def _handle_grep_or_glob(payload, tool_name) -> None:
    """Gate a repo-wide content/name search (Grep or Glob) the same way Read
    is gated, minus the byte-size threshold -- these tools have no single
    file whose size to check, so every call is the interesting case (the one
    Read only reaches once a file crosses the threshold). Recency scanning,
    the one-time marker/second-attempt override, and the empty-index check
    are all reused unchanged from the Read path via the shared helpers.
    """
    tool_input = payload.get("tool_input") or {}
    search_dir = tool_input.get("path") or payload.get("cwd") or "."
    pattern = tool_input.get("pattern") or ""
    target_key = f"{tool_name}:{search_dir}:{pattern}"

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
        # No single file is targeted, so "" is passed as the write-recency
        # target -- it can never equal a real Write/Edit file_path, so
        # is_recently_written is always False here; only the
        # has_recent_code_lookup half of the shared helper is used.
        _is_recently_written, has_recent_code_lookup = _parse_transcript(
            transcript_path, "", recent_n, tail_bytes)
    except Exception:
        return

    if has_recent_code_lookup:
        return

    repo_name = _find_repo_name(os.path.join(search_dir, "x"))
    if not _repo_has_chunks(repo_name):
        _log_decision(repo_name, tool_name, target_key, "pass-empty-index")
        return

    session_id = payload.get("session_id", "")
    marker = _state_dir(session_id) / hashlib.sha256(target_key.encode()).hexdigest()
    if marker.exists():
        _log_decision(repo_name, tool_name, target_key, "pass-second-attempt")
        return

    marker.touch()

    repo_arg = f', repo="{repo_name}"' if repo_name else ""
    call_hint = (
        f'mcp__ai-router__code_lookup(query="<what you\'re looking for>", '
        f'path_prefix="{search_dir}"{repo_arg})'
    )
    reason = (
        f"code_lookup gate: this {tool_name} search (pattern={pattern!r} under {search_dir}) "
        f"has not been preceded by mcp__ai-router__code_lookup in the last {recent_n} tool calls this session. "
        f"Call {call_hint} first "
        "— it returns only the relevant chunks instead of a repo-wide scan. If you deliberately need the raw "
        f"search, retry this exact {tool_name} call — the second attempt will pass."
    )

    _log_decision(repo_name, tool_name, target_key, "block")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))

if __name__ == "__main__":
    main()
