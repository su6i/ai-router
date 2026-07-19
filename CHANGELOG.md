# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); this project has no
tagged releases yet (see `README.md` § Status), so entries are grouped as
`Unreleased` until the first release cut.

## Unreleased

### Added

- **Phase 3b: Code-Aware RAG (`r code` / `code_lookup`)** — semantic
  retrieval over code: git-tracked `*.py`/`*.sh` files are chunked at
  function/class/method boundaries with tree-sitter (oversized defs split at
  block boundaries, re-prefixed with their signature), embedded with the
  local e5-small ONNX model, and stored in pgvector (`code_chunks`, HNSW)
  next to a static Python call graph (`code_edges`, stdlib `ast`).
  `r code "<query>" [-k N] [--graph] [--repo PATH]` returns chunks with
  `path:start-end` refs capped at ~2k tokens; `--graph` adds 1-hop
  callers/callees; `--reindex` is incremental by `git diff` + `chunk_hash`
  upsert (idempotent), `--rebuild` full. Exposed to MCP hosts as
  `code_lookup`. tree-sitter is pinned `>=0.25,<0.26` and chunking runs in
  an isolated child process — py-tree-sitter 0.26.0 deterministically
  segfaulted on macOS arm64 when live tokenizers/onnxruntime objects
  coexisted with AST walks (three independent repros; documented in
  `docs/CODE-RAG.md` with the honest "when it pays off" economics and a
  measured −90.8% briefing-token delta vs whole-file context).
  New deps (pre-approved in wo-0013): `tree-sitter`, `tree-sitter-python`,
  `tree-sitter-bash`.

### Changed

- **Copilot default model → `gpt-5-mini`** — `gpt-5-mini` has a 0×
  premium-request multiplier on Copilot Pro (per GitHub docs "Requests in
  GitHub Copilot", verified 2026-07-18), so default worker calls no longer
  consume the 300 premium requests/month. Harder tasks escalate explicitly
  via `--model gpt-5` or `--model claude-sonnet-4.5`. Premium
  request accounting now records the model's multiplier instead of
  counting every copilot call as `1`. The default worker channel
  remains `agy` (Gemini 3.1 Pro).
- **Copilot multipliers are config, not code** — per-model premium-request
  multipliers moved out of the source into `<data>/copilot_multipliers.json`
  (seeded on first copilot call), because GitHub changes rates without notice
  and exposes no API for them (live-checked 2026-07-19: `seat_info`/copilot
  usage endpoints are org-only and 404 on a personal plan; the
  `copilot_internal/v2/token` exchange rejects CLI tokens). Unknown models
  bill at the file's `default` (1×) — never silently free. `r cost` now also
  queries the GitHub billing API for the month's **Copilot overage actually
  billed** (needs the gh `user` scope); `$0` = inside quota, non-zero = quota
  exceeded and paying — the cue to reconcile the multiplier file.

### Fixed

- **`r code` / `r rules` from any directory** — these run under the project
  venv via `uv run` now, instead of the stdlib-only `python3` used for
  chat/audit; previously they crashed with `ModuleNotFoundError: psycopg`
  when invoked outside the repo. When Postgres is unreachable they print a
  one-line hint (`start it first: colima start`) instead of a raw traceback.
- **Ingest integration test skips when Postgres is down** —
  `test_integration_ingest_idempotent` now probes a real connection (and loads
  the vault env) instead of only checking `POSTGRES_DSN`, so a stopped Colima
  yields a skip, not a failure.

### Fixed

- **agy headless permission auto-denial** — since agy 1.1.3 (2026-07-16),
  `--mode accept-edits` no longer auto-approves `write_file`/`command` in
  print mode; every headless `delegate_agent` run died in 18–41 s with
  `permission check failed … auto-denied` and work fell back to metered
  DeepSeek. Router-managed headless launches now pass the documented
  `--dangerously-skip-permissions` flag (managed launches only, never
  interactive sessions; agy has no settings file for scoped allow-rules).
  Live acceptance: branch + file edit + commit in a scratch repo via
  `delegate_agent`, COMPLETED in 25.5 s, $0.

### Security

- **API key removed from gemini URL** — `call_gemini` now sends the key via
  the `x-goog-api-key` header instead of a `?key=` query parameter. A stale
  MCP server process (running pre-retry code) leaked the full keyed URL to a
  calling agent on 2026-07-15 via an `HTTPStatusError` message.
- **MCP error redaction** — every JSON-RPC error message leaving
  `mcp/server.py` is scrubbed: `key=` query params and the values of all
  loaded provider keys are replaced with placeholders (defense in depth).
- **Key fingerprint log dropped** — the DEBUG log no longer prints partial
  key characters, only the key length.

### Added

- **Channel Registry & New Subscription Runners** — Introduced a channel registry (configured via `channels.json` and `AI_ROUTER_DISABLE_CHANNELS` env var) to toggle access to individual execution channels (e.g. `agy`, `codewhale`, `codex`, `copilot`). Added support for delegating to GitHub Copilot (`copilot`) and OpenAI Codex (`codex`) CLI tools in `delegate_agent`. Reusing existing subscriptions ensures no marginal cost, so their usage is logged as $0 in `audit.log`. For `copilot`, added an optional budget cap `copilot_premium_requests_month` and per-request tracking to prevent overutilization of the premium request allocation. You can inspect all available channels and their authentication status with the new `r channels` (or `--channels`) CLI command.
- **Delivery Gate (`scripts/verify_delivery.sh`)** — A mechanical pipeline gate implementing the strict runlog constraints of EXECUTOR-RUNLOG.md, guarding the `feat/router-only-workers` branch against premature or incomplete deliveries.
- **Worker channel nudge hook (`hooks/worker_channel_nudge.py`)** — A PreToolUse hook preventing headless workers (`agy print`, `codewhale`) from being launched directly in the terminal, nudging the caller to use the router (`delegate_agent`). Allows a deliberate second attempt.
- **Per-channel system prompts (`templates/system-prompts/*.md`)** — Dynamically injects channel-specific instructions at the very top of worker prompts to establish worker persona. Includes templates for `gemini`, `deepseek`, and `minimax`.
- Phase 3a: Rules retrieval index (`r rules`) using local `intfloat/multilingual-e5-small` ONNX model and `pgvector` for semantic context loading instead of passing whole files. Exposed via `r rules` CLI and `rules_lookup` MCP tool. Translations (`docs/fa/`, `*.fa.md`) are excluded from the corpus so cross-lingual queries reach the canonical English rules.

### Changed

- **Observability Polish** — Replaced raw prints with Python `logging` in `delegate.py` (`--quiet` flag suppresses INFO). Diagnostic messages (budget notices, fallback warnings) now use proper log levels. The key fingerprint line is now stderr-only DEBUG to prevent leaks.
- **MCP Server Diagnostics** — Added stderr logging for incoming requests (`[req <id>] <method> <tool> model=<m>`) to `mcp/server.py` without polluting the JSON-RPC stdout channel.
- **Encapsulation & Entry Point** — Moved `_last_audit_cost` into `delegate.py` as `get_last_cost`. Replaced `main.py` stub with a real entry point.
- **Docs Reconciliation** — Updated `ARCHITECTURE.md` with a "Current state vs plan" table. Marked Phase 3 RAG/prompt_cache as rejected per project rules. Added documentation for the sentinel-line protocol and `shell=True` verify commands in `delegate.py`. Cross-linked `CLAUDE.md` and `ARCHITECTURE.md`.

### Fixed

- **CI hermeticity** — First CI run on `main` exposed two vault-machine
  assumptions: the MCP budget-abort test reached the provider key check before
  the budget check on runners without the vault `.env` (fixed by injecting fake
  `DEEPSEEK_API_KEY`/`MINIMAX_API_KEY` into the test server env), and the 6
  zsh-parametrized `test_r_wrapper` cases silently dropped off ubuntu-latest
  (fixed by installing zsh in the workflow). CI now runs the same 73 tests as
  local.

### Added

- **`delegate_agent` (CodeWhale exec / agy headless)** — A third delegation door for multi-step grunt tasks that need exploration and iterative debugging (where the exact files aren't known upfront). Wraps either `agy` (Gemini 3.1 Pro, $0 subscription quota, default) or `codewhale exec` (DeepSeek/MiniMax, paid fallback) behind the same budget caps and audit ledger. Exposed via the CLI (`r agent "<task>"`) and as an MCP tool in `mcp/server.py`. Returns a ≤25-line summary of files changed, verify result, and cost.
- **Worker context discipline pack and repo map** — A new `AGENTS-context-discipline.md` template defines strict file-reading rules (read whole once, use `grep -n`, batch reads). A condensed version of these rules is now injected as a cache-friendly constant preamble in all worker and agent prompts. A new `src/repo_map.py` script automatically generates a compact (`< 4000` chars) repository map of top-level symbols, which is also prepended to all prompts.

- **Quota-channel daily call caps** — Added optional `"daily_calls": {"google-ai-pro": 50, "gemini-free": 400}` to `budgets.json`. These enforce strict limits on the number of local delegated calls per `quota_channel` per calendar day. Free/subscription channels (where `cost_usd=0`) are now gated by this quota. Every audit row now includes a `quota_channel` field.

- **Data plane Phase 1** — `db/init/01_schema.sql` defines the `usage` table for Postgres (auto-applied via Docker Compose). `src/ingest.py` provides an idempotent ingest from `audit.log` into Postgres using `psycopg[binary]`.

- **Provider prompt caching accounting** — Added explicit accounting for API provider-level prefix caching (DeepSeek, Gemini, MiniMax). `MODELS` pricing now supports `cin_cached` for discounted cache-read billing. Cache hits and misses are parsed directly from provider usage metrics and logged to the audit trail (including `cache_miss` if available). Cost calculations strictly apply `cin_cached` or `cin` accordingly. The CLI summary and `worker` blocks now report `cache hit rate: NN.N%`. Worker mode enforces prefix discipline by appending the task string *after* file contents to maximize cache hits.

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
 

- Fix: Redact API keys from router logs and error messages
