"""Tests for mcp/server.py — WO6, MCP-lite server exposing capped delegate
tools (delegate_research, delegate_worker) over stdio JSON-RPC.

Wire format verified against the Model Context Protocol specification
revision 2025-11-25 (see comment at the top of mcp/server.py).

No network calls, no real `agy` subprocess: the server subprocess is booted
through a tiny bootstrap script that monkeypatches delegate.call_gemini
(canned stub — kept in delegate.py for research-tool tests via a throwaway
"test-gemini" MODELS entry, owner decree 2026-07-27 removed the real
free-quota "gemini" entry but not the plumbing), delegate.call_agy_print
(canned stub for the new agy worker-backend default), and delegate.call_openai
(hard-fails if ever invoked) before running server.main(). This is required,
not optional: server.main() calls delegate.load_env(), which reloads real
provider keys from the shared vault
(~/.local/share/agent-projects/_shared/secrets/.env) regardless of what this
test's subprocess env sets or unsets — so any un-stubbed call path would be a
live, billable request. GEMINI_API_KEY is also set to a placeholder so
delegate.py's missing-key guard does not fire; AI_ROUTER_DATA_DIR points at a
tmp vault so the audit ledger/cache never touch the real vault.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
MCP_DIR = REPO_ROOT / "mcp"

BOOTSTRAP_TEMPLATE = """
import sys
sys.path.insert(0, {src!r})
sys.path.insert(0, {mcp_dir!r})
import delegate as d

# call_gemini()/provider=="gemini" is deliberately kept in delegate.py (owner
# decree 2026-07-27 removed the free-quota "gemini" MODELS entry, not the
# plumbing) -- register a throwaway MODELS entry so delegate_research tests
# can still exercise it via the canned fake below.
d.MODELS["test-gemini"] = {{
    "api": "test-gemini-model", "provider": "gemini",
    "url": "https://generativelanguage.googleapis.com/v1beta",
    "cin": 0.0, "cout": 0.0, "key": "GEMINI_API_KEY",
    "quota_channel": "test-free",
}}
d.ALIASES["test-gemini"] = "test-gemini"

_RESPONSES = {responses!r}
_calls = {{"n": 0}}

def _fake_call_gemini(spec, key, history, system, max_output_tokens=8192):
    text = _RESPONSES[min(_calls["n"], len(_RESPONSES) - 1)]
    _calls["n"] += 1
    if text == "__RAISE__":
        raise RuntimeError("stubbed provider failure")
    return (text.format(max_output_tokens=max_output_tokens), spec["api"],
            "resp-%d" % _calls["n"], 10, 5, 0, None)

def _fake_call_agy_print(prompt, model_name, project_root, timeout_s):
    text = _RESPONSES[min(_calls["n"], len(_RESPONSES) - 1)]
    _calls["n"] += 1
    if text == "__RAISE__":
        raise RuntimeError("stubbed provider failure")
    return (text, model_name, None, 0, 0, 0, None)

def _fake_call_openai(spec, key, history, system, max_output_tokens=8192):
    # Hard safety net: this test suite must make zero real provider calls.
    # load_env() (called by server.main()) reloads real keys from the shared
    # vault regardless of this subprocess's env, so any un-stubbed path here
    # would be a live, billable request.
    raise AssertionError("call_openai must never be invoked in tests")

d.call_gemini = _fake_call_gemini
d.call_agy_print = _fake_call_agy_print
d.call_openai = _fake_call_openai

def _fake_agent_delegate(*args, **kwargs):
    return "agent summary"
d.agent_delegate = _fake_agent_delegate

class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
    def json(self):
        return self._json

def _fake_httpx_post(url, *args, **kwargs):
    if "sendMessage" in url:
        return FakeResponse({{"ok": True, "result": {{"message_id": 999}}}})
    if "pinChatMessage" in url:
        return FakeResponse({{"ok": True, "result": {{}}}})
    return FakeResponse({{"ok": False}})
import httpx
httpx.post = _fake_httpx_post

import server
server.main()
"""


def _spawn_server(tmp_path, responses, extra_env=None):
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text(BOOTSTRAP_TEMPLATE.format(
        src=str(SRC_DIR), mcp_dir=str(MCP_DIR), responses=responses))
    env = dict(os.environ)
    env["AI_ROUTER_DATA_DIR"] = str(tmp_path / "vault")
    env["GEMINI_API_KEY"] = "fake-key-for-tests"
    # On machines without the vault (CI), load_env() finds no .env, so these
    # fakes are what keeps the server from failing the key check before it
    # reaches the code path under test. On dev machines the vault overrides.
    env["DEEPSEEK_API_KEY"] = "fake-key-for-tests"
    env["MINIMAX_API_KEY"] = "fake-key-for-tests"
    env["AI_ROUTER_BOT_TOKEN"] = "fake-bot-token"
    env["TELEGRAM_OWNER_CHAT_ID"] = "fake-chat-id"
    env.pop("GROK_API_KEY", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(bootstrap)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env)


def _send(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _recv(proc):
    line = proc.stdout.readline()
    assert line, f"no response from server (stderr: {proc.stderr.read()})"
    return json.loads(line)


def _init(proc):
    _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "test-client", "version": "0.0.1"}}})
    resp = _recv(proc)
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return resp


@pytest.fixture
def server_proc(tmp_path):
    proc = _spawn_server(tmp_path, ["stub response"])
    yield proc
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_initialize_advertises_tools_capability(server_proc):
    resp = _init(server_proc)
    assert resp["id"] == 1
    assert "error" not in resp
    result = resp["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]


def test_tools_list_exposes_exactly_tools(server_proc):
    _init(server_proc)
    _send(server_proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = _recv(server_proc)
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"delegate_research", "delegate_worker", "delegate_agent", "rules_lookup", "code_lookup",
                      "send_note", "list_notes", "route_task", "send_to_owner", "dashboard_push"}

    research = next(t for t in tools if t["name"] == "delegate_research")
    assert set(research["inputSchema"]["properties"]) == {
        "question", "model", "max_output_tokens", "search", "max_tool_calls"}

    worker = next(t for t in tools if t["name"] == "delegate_worker")
    assert set(worker["inputSchema"]["properties"]) == {
        "prompt", "files", "allow_write", "verify", "model", "retries", "workdir"}


def test_tools_call_delegate_research_returns_capped_answer_with_cost(tmp_path):
    proc = _spawn_server(tmp_path, ["research answer (cap={max_output_tokens})"])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "delegate_research",
                                "arguments": {"question": "what is 2+2?",
                                              "model": "test-gemini",
                                              "max_output_tokens": 321}}})
        resp = _recv(proc)
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "research answer (cap=321)" in text
        assert "cost:" in text
        assert resp["result"]["isError"] is False
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_tools_call_delegate_worker_writes_file_within_workdir(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    response = (
        "===FILE: src/foo.py===\n"
        "def foo():\n"
        "    return 1\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "added foo()\n"
        "===END SUMMARY===\n"
    )
    proc = _spawn_server(tmp_path, [response])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "delegate_worker",
                                "arguments": {"prompt": "add foo()",
                                              "files": "src/foo.py",
                                              "allow_write": "src/**",
                                              "verify": "true",
                                              "retries": 1,
                                              "workdir": str(workdir)}}})
        resp = _recv(proc)
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert len(text.splitlines()) <= 25
        assert (workdir / "src" / "foo.py").read_text() == "def foo():\n    return 1\n"
        assert not (tmp_path / "src").exists()  # nothing written outside workdir
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_tools_call_delegate_worker_defaults_to_agy(tmp_path):
    # WO-0023 requirement: delegate_worker's MCP default model must be
    # "agy" (the $0 coding default), not the removed "gemini".
    workdir = tmp_path / "project2"
    workdir.mkdir()
    response = "===FILE: src/bar.py===\nx = 1\n===END FILE===\n===SUMMARY===\nok\n===END SUMMARY===\n"
    proc = _spawn_server(tmp_path, [response])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                     "params": {"name": "delegate_worker",
                                "arguments": {"prompt": "add bar()",
                                              "files": "src/bar.py",
                                              "allow_write": "src/**",
                                              "workdir": str(workdir)}}})
        resp = _recv(proc)
        assert "error" not in resp
        assert (workdir / "src" / "bar.py").read_text() == "x = 1\n"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_tools_call_delegate_agent_returns_summary(tmp_path):
    proc = _spawn_server(tmp_path, [])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "delegate_agent",
                                "arguments": {"prompt": "do agent task",
                                              "workdir": str(tmp_path),
                                              "runner": "agy"}}})
        resp = _recv(proc)
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "agent summary" in text
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_tools_call_delegate_failure_returns_jsonrpc_error(tmp_path):
    # The stubbed provider raises — this must surface as a loud JSON-RPC
    # error, never a silent/successful result.
    proc = _spawn_server(tmp_path, ["__RAISE__"])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "delegate_research",
                                "arguments": {"question": "anything", "model": "test-gemini"}}})
        resp = _recv(proc)
        assert "result" not in resp
        assert resp["error"]["code"] < 0
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_tools_call_budget_abort_returns_jsonrpc_error(tmp_path):
    # Create budget file and audit log that exceeds it
    vault = tmp_path / "vault" / "data"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "budgets.json").write_text('{"monthly_usd": 1.0}')
    (vault / "audit.log").write_text(json.dumps({
        "ts": "2026-07-14T00:00:00+00:00",
        "cost_usd": 1.5,
    }) + "\n")

    proc = _spawn_server(tmp_path, ["won't be called"])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "delegate_research",
                                "arguments": {"question": "anything", "model": "flash"}}})
        resp = _recv(proc)
        assert "result" not in resp
        assert resp["error"]["code"] == -32000
        assert "BUDGET ABORT: monthly_usd cap exceeded" in resp["error"]["message"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_sequential_tools_call_behaves_correctly(tmp_path):
    proc = _spawn_server(tmp_path, ["ans1", "ans2"])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "delegate_research",
                                "arguments": {"question": "q1", "model": "test-gemini"}}})
        resp1 = _recv(proc)
        assert resp1["result"]["content"][0]["text"].startswith("ans1")

        _send(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                     "params": {"name": "delegate_research",
                                "arguments": {"question": "q2", "model": "test-gemini"}}})
        resp2 = _recv(proc)
        assert resp2["result"]["content"][0]["text"].startswith("ans2")
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_tools_call_unknown_tool_returns_jsonrpc_error(server_proc):
    _init(server_proc)
    _send(server_proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                        "params": {"name": "no_such_tool", "arguments": {}}})
    resp = _recv(server_proc)
    assert "result" not in resp
    assert resp["error"]["code"] == -32602


def test_tools_call_malformed_params_returns_jsonrpc_error(server_proc):
    _init(server_proc)
    _send(server_proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                        "params": {"name": "delegate_research",
                                   "arguments": {"question": "hi", "max_output_tokens": 999999}}})
    resp = _recv(server_proc)
    assert "result" not in resp
    assert resp["error"]["code"] == -32602


def test_tools_call_dashboard_push_returns_summary(tmp_path):
    proc = _spawn_server(tmp_path, [])
    try:
        _init(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "dashboard_push",
                                "arguments": {"kind": "both"}}})
        resp = _recv(proc)
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "inbox: created (message_id=999)" in text
        assert "tasks: created (message_id=999)" in text
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
