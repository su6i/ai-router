#!/usr/bin/env python3
"""mcp/server.py — hand-rolled stdio JSON-RPC MCP server exposing capped
delegate.py tools (WO6, MCP-SERVER-DESIGN.md).

Wire format verified against the official Model Context Protocol
specification, revision 2025-11-25
(https://modelcontextprotocol.io/specification/2025-11-25) — the latest
ratified spec as of 2026-07-03 (a 2026-07-28 release candidate exists but is
not yet final). Three methods only: initialize, tools/list, tools/call, plus
the notifications/initialized notification, newline-delimited JSON-RPC 2.0
over stdio. Stdlib-only per design Decision 1 (hand-rolled over the `mcp`
SDK — no new dependency).

Golden rule: cheap-model output must never flood the caller's context.
delegate_worker returns only the existing <=25-line summary contract (file
contents never cross the wire); delegate_research is capped by
max_output_tokens (request parameter, low default). No uncapped chat tool,
ever — see "Non-goals" in the design doc.
"""
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d  # noqa: E402

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "ai-router-mcp"
SERVER_VERSION = "0.1.0"

# JSON-RPC error codes (spec-reserved ranges).
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
PARSE_ERROR = -32700
SERVER_ERROR = -32000  # implementation-defined: the delegate call itself failed

TOOLS = [
    {
        "name": "delegate_research",
        "description": ("Ask a cheap/fast model a fact-lookup or live-data question "
                         "(default grok = live web/X search). USE THIS INSTEAD of "
                         "WebSearch/WebFetch or answering from memory whenever the "
                         "question is: a current fact, a version/license/API check, "
                         "or doc verification — a ~$0.003 call beats burning premium "
                         "context. Answer is capped at max_output_tokens. Never for "
                         "bulk chat."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "model": {"type": "string", "default": "grok",
                          "description": "router alias; grok = live web/X search"},
                "max_output_tokens": {"type": "integer", "default": 500, "maximum": 2000},
            },
            "required": ["question"],
        },
    },
    {
        "name": "delegate_worker",
        "description": ("Delegate grunt coding to a cheap model that reads and writes "
                         "the files on disk ITSELF — file contents never enter your "
                         "context; only a <=25-line summary comes back. USE THIS "
                         "INSTEAD OF Edit/Write whenever the task is: new "
                         "implementation over ~40 lines, test files, boilerplate, or "
                         "the same mechanical change across 2+ files. Golden rule: "
                         "call it BEFORE reading the target files — pass paths, not "
                         "contents. Model ladder: gemini (free, default) -> flash/pro "
                         "(DeepSeek) when gemini fails verify. Always pass verify "
                         "(e.g. 'uv run pytest -q') when the repo has tests. Claude "
                         "models are never reachable here."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "files": {"type": "string",
                          "description": "comma-separated, as --files"},
                "allow_write": {"type": "string",
                                "description": "globs, as --allow-write"},
                "verify": {"type": "string", "default": ""},
                "model": {"type": "string", "default": "gemini"},
                "retries": {"type": "integer", "default": 1, "maximum": 2},
                "workdir": {"type": "string",
                            "description": "absolute path of the repo the files live in"},
            },
            "required": ["prompt", "workdir"],
        },
    },
    {
        "name": "delegate_agent",
        "description": "USE for multi-step grunt tasks needing exploration (find+fix across unknown files, iterative debugging); prefer delegate_worker when the file list is already known.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "workdir": {"type": "string", "description": "absolute path"},
                "model": {"type": "string"},
                "runner": {"type": "string", "default": "agy"},
                "verify": {"type": "string", "default": ""},
                "timeout": {"type": "integer", "default": 600, "maximum": 1800},
            },
            "required": ["prompt", "workdir"],
        },
    },
    {
        "name": "rules_lookup",
        "description": ("Retrieve only the most relevant rule/doc chunks "
                         "(constitution rules, project docs) for a query — "
                         "USE THIS instead of reading whole rule files; "
                         "output is capped at ~2k tokens."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
]


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _rpc_result(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": _redact(message)}}


# key=... query params (defense in depth: keys should never be in URLs, but a
# stale server process leaked one via an httpx exception message on 2026-07-15)
_KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s'\"]+")


def _redact(text: str) -> str:
    """Scrub secrets from any text that leaves the server over the wire."""
    text = _KEY_PARAM_RE.sub(r"\1<redacted>", text)
    for env_name in {spec["key"] for spec in d.MODELS.values()}:
        value = os.environ.get(env_name)
        if value:
            text = text.replace(value, f"<{env_name}>")
    return text





def handle_delegate_research(args: dict) -> dict:
    question = args.get("question")
    if not question or not isinstance(question, str):
        raise ValueError("'question' is required and must be a non-empty string")
    max_output_tokens = args.get("max_output_tokens", 500)
    if not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool) \
            or not (0 < max_output_tokens <= 2000):
        raise ValueError("'max_output_tokens' must be an integer in (0, 2000]")
    model = d.resolve_model(args.get("model", "grok"))

    with contextlib.redirect_stdout(io.StringIO()):
        answer = d.delegate(question, model, max_output_tokens=max_output_tokens, via="mcp")
    cost = d.get_last_cost()
    return _text_result(f"{answer}\n\ncost: ${cost:.6f}")


def handle_delegate_worker(args: dict) -> dict:
    prompt = args.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise ValueError("'prompt' is required and must be a non-empty string")
    workdir = args.get("workdir")
    if not workdir or not isinstance(workdir, str) or not Path(workdir).is_absolute():
        raise ValueError("'workdir' is required and must be an absolute path")
    retries = args.get("retries", 1)
    if not isinstance(retries, int) or isinstance(retries, bool) or not (0 <= retries <= 2):
        raise ValueError("'retries' must be an integer in [0, 2]")
    model = d.resolve_model(args.get("model", "gemini"))

    with contextlib.redirect_stdout(io.StringIO()):
        summary = d.worker_delegate(
            prompt, model, args.get("files", ""), args.get("allow_write", ""),
            args.get("verify", ""), retries, project_root=Path(workdir), via="mcp")
    return _text_result(summary)


def handle_delegate_agent(args: dict) -> dict:
    prompt = args.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise ValueError("'prompt' is required and must be a non-empty string")
    workdir = args.get("workdir")
    if not workdir or not isinstance(workdir, str) or not Path(workdir).is_absolute():
        raise ValueError("'workdir' is required and must be an absolute path")
    timeout = args.get("timeout", 600)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not (0 < timeout <= 1800):
        raise ValueError("'timeout' must be an integer in (0, 1800]")
    
    runner = args.get("runner", "agy")
    model = args.get("model")
    verify = args.get("verify", "")

    with contextlib.redirect_stdout(io.StringIO()):
        summary = d.agent_delegate(
            prompt, runner=runner, model=model, workdir=workdir, verify_cmd=verify,
            via="mcp", timeout_s=timeout)
    return _text_result(summary)

def handle_rules_lookup(args: dict) -> dict:
    query = args.get("query")
    if not query or not isinstance(query, str):
        raise ValueError("'query' is required and must be a non-empty string")
    k = args.get("k", 5)
    if not isinstance(k, int) or isinstance(k, bool) or not (0 < k <= 20):
        raise ValueError("'k' must be an integer in (0, 20]")

    # src/ is already on sys.path (top of this file) — importing as
    # "rules_index" keeps delegate a single module identity in this process.
    import rules_index as ri

    class Args:
        pass
    a = Args()
    a.query = query
    a.k = k

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ri.cmd_search(a)
    return _text_result(out.getvalue())


TOOL_HANDLERS = {
    "delegate_research": handle_delegate_research,
    "delegate_worker": handle_delegate_worker,
    "delegate_agent": handle_delegate_agent,
    "rules_lookup": handle_rules_lookup,
}


def handle_tools_call(id_, params: dict):
    name = (params or {}).get("name")
    arguments = (params or {}).get("arguments") or {}
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _rpc_error(id_, INVALID_PARAMS, f"Unknown tool: {name}")
    try:
        result = handler(arguments)
    except ValueError as e:
        return _rpc_error(id_, INVALID_PARAMS, str(e))
    except SystemExit as e:
        return _rpc_error(id_, SERVER_ERROR, str(e.code) if e.code else "delegate exited")
    except Exception as e:  # noqa: BLE001 — fail loud over the wire, never swallow
        return _rpc_error(id_, SERVER_ERROR, f"{type(e).__name__}: {e}")
    return _rpc_result(id_, result)


def handle_request(msg: dict):
    """Returns a JSON-RPC response dict, or None for notifications (per spec,
    the server MUST NOT reply to a message with no 'id')."""
    method = msg.get("method")
    is_notification = "id" not in msg
    id_ = msg.get("id")

    if method == "tools/call":
        params = msg.get("params") or {}
        tool = params.get("name")
        args = params.get("arguments") or {}
        m = args.get("model")
        if not m:
            if tool == "delegate_research":
                m = "grok"
            elif tool == "delegate_worker":
                m = "gemini"
            elif tool == "delegate_agent":
                m = "None"
        print(f"[req {id_}] {method} {tool} model={m}", file=sys.stderr)
    elif method and not is_notification:
        print(f"[req {id_}] {method} - model=-", file=sys.stderr)

    if method == "initialize":
        return _rpc_result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _rpc_result(id_, {"tools": TOOLS})
    if method == "tools/call":
        return handle_tools_call(id_, msg.get("params"))
    if is_notification:
        return None
    return _rpc_error(id_, METHOD_NOT_FOUND, f"Method not found: {method}")


def main():
    d.load_env()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_rpc_error(None, PARSE_ERROR, "Parse error")), flush=True)
            continue
        resp = handle_request(msg)
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
