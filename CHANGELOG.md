# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); this project has no
tagged releases yet (see `README.md` § Status), so entries are grouped as
`Unreleased` until the first release cut.

## Unreleased

### Fixed

- **CI hermeticity** — First CI run on `main` exposed two vault-machine
  assumptions: the MCP budget-abort test reached the provider key check before
  the budget check on runners without the vault `.env` (fixed by injecting fake
  `DEEPSEEK_API_KEY`/`MINIMAX_API_KEY` into the test server env), and the 6
  zsh-parametrized `test_r_wrapper` cases silently dropped off ubuntu-latest
  (fixed by installing zsh in the workflow). CI now runs the same 73 tests as
  local.

### Added

- **Cache hygiene and pruning** — Improved the exact-hash cache with NFC unicode normalization to ignore character composition differences. Added `max_output_tokens` to the cache key to correctly distinguish calls that request different output lengths (NOTE: this invalidates all existing cache entries). Implemented a silent automatic cache pruning policy enforcing a maximum of 5,000 rows and 90 days retention to prevent unbounded growth. Added a `--cache-prune` CLI flag to trigger this cleanup manually and inspect the row count.

- **Formalized dependencies and CI/CD** — Declared runtime dependencies (`httpx`) and dev dependencies (`pytest`) in `pyproject.toml`. Added GitHub Actions workflow (`.github/workflows/test.yml`) for automated testing and code quality checks using `uv` and `ruff`. Added comprehensive offline tests covering `call_openai`, audit reporting, `project_info`, and MCP server edge cases.

- **`r cost` (Cost Report)** — A new CLI subcommand (`python3 src/delegate.py --cost` and `r cost`) to aggregate `audit.log` into an aligned text table of spend and cache hit rates. Supports time filtering (`--since YYYY-MM-DD`, `--today`) and custom groupings (`--by model|project|session|via|day`).

- **Provider resilience & automatic fallbacks** — Added automatic retries with exponential backoff for transient errors (HTTP 429, 5xx, or timeouts) and clear `ProviderError` exceptions for hard failures (missing response fields or HTTP 4xx). Added an automatic fallback to `flash` for `gemini` if the free tier rate limit is exhausted, mirroring the existing fallback behavior for `minimax` credit exhaustion. Replaced `sys.exit` in `resolve_model` with a `ValueError` so invalid models correctly map to JSON-RPC `INVALID_PARAMS` errors in the MCP server.

- **Budget caps & cost estimates (`--estimate`)** — Implemented fail-loud budget caps for the router. Budgets are defined in `<vault>/data/budgets.json` (monthly, weekly, per-session, per-project). If any cap is exceeded, the router aborts the call and exits with an error (which surfaces as a JSON-RPC error in the MCP path). If usage reaches 80% of a cap, a warning is printed to stderr. Added `--estimate` flag to dry-run calls, returning estimated token usage and cost alongside current budget spend without hitting the provider or writing to the audit log.

- **Delegation triggers — imperative tool descriptions + PreToolUse nudge
  hook.** The MCP tools existed but the premium architect model never
  called them; two layers now push it toward the worker. (1) Both tool
  descriptions in `mcp/server.py` are rewritten imperatively: they state
  when to use the tool *instead of* Edit/Write or WebSearch (implementation
  over ~40 lines, test files, mechanical multi-file changes; live facts /
  doc verification) and carry the golden rule — decide before reading the
  target files, pass paths not contents. (2) New `hooks/delegate_nudge.py`,
  a Claude Code PreToolUse hook (registered globally, matcher `Write|Edit`)
  that denies the first large code write (> 40 new lines, code suffixes
  only; docs/config/scratchpad exempt) with a delegation reminder; a second
  attempt on the same file passes (escape hatch for architecture-critical
  code); fail-open on any hook error. The routing policy itself moved from
  a one-line hint to a decision protocol in the global `~/.claude/CLAUDE.md`
  (Cost Routing section).

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
 
