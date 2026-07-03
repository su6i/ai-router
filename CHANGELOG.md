# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); this project has no
tagged releases yet (see `README.md` § Status), so entries are grouped as
`Unreleased` until the first release cut.

## Unreleased

### Added

- **`mcp/server.py` — MCP-lite server.** Hand-rolled stdio JSON-RPC server
  (stdlib-only, no new dependency; protocol revision 2025-11-25) exposing
  `delegate.py` as two capped MCP tools: `delegate_research` (fact lookup,
  answer capped by `max_output_tokens`, default model `grok`) and
  `delegate_worker` (grunt coding work, same `--files`/`--allow-write`/
  `--verify`/`--retries` contract as CLI worker mode plus a required
  `workdir`, default model `gemini`). No uncapped chat tool — the golden
  rule (cheap-model output must never flood the caller's context) holds for
  both doors. Register once at user scope:
  `claude mcp add --scope user ai-router -- python3 /Users/su6i/@-github/ai-router/mcp/server.py`.
  `delegate.py` gained an optional `max_output_tokens: int = 8192` parameter
  threaded into `call_openai`/`call_gemini` (gemini: previously uncapped,
  now defaults to the same cap as openai; CLI/`r()` unaffected — no new
  flag) and an optional `via` parameter on `delegate()`/`worker_delegate()`
  so MCP-originated audit rows carry `via: "mcp"` (the field is absent, not
  null, for `r()`/CLI rows). Tests: `tests/test_mcp_server.py`, subprocess
  the server over real stdio with both providers stubbed, zero paid calls.

- **`shell/r.sh` — the `r()` shell wrapper.** One `source` line in a shell
  rc gives `r <model> <prompt…>` (chat), `r <model> --<flags…>` (raw
  passthrough, worker mode included) and `r audit` from any directory, so
  grunt work reaches `delegate.py` without entering an agent's context.
  The wrapper holds no routing/cost logic; first argument is always the
  model and unknown names fail loudly. Env overrides `AI_ROUTER_REPO` /
  `AI_ROUTER_PYTHON`; tested against a stub delegate on both bash and zsh
  (`tests/test_r_wrapper.py`, zero paid calls).

### Docs

- README.md/README.fa.md rewritten with a real Usage guide (one-shot chat,
  sessions, cache behavior + `--no-cache`, worker mode with a full
  `--files`/`--allow-write`/`--verify`/`--retries` example and the actual
  output shape, `--audit`), a Models table sourced from `MODELS` in
  `src/delegate.py` (provider, cost in/out, role), a prominent link to
  `docs/ARCHITECTURE.md` at the top of the file, and a Testing section with
  an absolute-path command.

### Added

- **Worker mode (`delegate.py --files`)** — a cheap model can now read and
  rewrite files on disk directly instead of returning code as chat text.
  Wire protocol: sentinel-line blocks (`===FILE: path===` / `===END FILE===`
  / `===SUMMARY===`), never markdown fences. Writes are gated by
  `--allow-write` globs (no flag = no writes) with path-safety checks
  (rejects absolute paths, `..`, anything outside the allow-list).
  `--verify` runs a caller-supplied shell command after writing, with up to
  2 retries on failure. Only a short (≤25-line) summary — files written,
  verify result, worker's own summary, cost — ever reaches the caller; the
  generated code itself never does. Audit ledger gained `mode`,
  `files_written`, `files_rejected`, `verify_cmd`, `verify_status`,
  `attempts` columns.

### Changed

- **`delegate.py` moved into this repo** (`src/delegate.py`, tests in
  `tests/`) from the earlier `_router/` scratch location. Runtime state —
  cache, audit log, session memory — resolves through the rule-035 vault
  (`~/.local/share/agent-projects/ai-router/data/`, override with
  `AI_ROUTER_DATA_DIR`) and is never committed. The old `_router/delegate.py`
  path is now a thin deprecation shim.
