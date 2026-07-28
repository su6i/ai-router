# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); this project has no
tagged releases yet (see `README.md` § Status), so entries are grouped as
`Unreleased` until the first release cut.

## Unreleased

### Changed

- **The free-quota Gemini API channel is removed (owner decree 2026-07-27).**
  `gemini`, `gemini-lite` and `gemma` no longer exist as models. Asking for one
  of those names does **not** silently remap: `resolve_model()` raises a
  `ValueError` naming `agy` as the replacement, because a silent remap would
  hide from the caller that the model they benchmarked against is gone. The
  free-quota channel repeatedly defeated the `$0-first` ladder:
  `delegate_worker` could not accept `agy` at all (agy was a runner, not an
  HTTP backend), so its default fell through to free-quota `gemini-2.5-flash`
  and the owner's "coding default = agy" ruling never actually took effect.

- **`agy` is now a `delegate_worker` backend** via `call_agy_print()`, closing
  that gap, and is the default coding model. Determinism is preserved exactly
  as for the HTTP worker models: the *router*, not agy, applies file writes.
  Worker mode therefore runs agy with `--mode plan` and deliberately **without**
  `--add-dir`, so agy cannot write to the repo even if it tries — the opposite
  of `agent_delegate`, where agy *is* the writer and `--add-dir` is required.

- **No automatic fallback from a $0 channel to a paid one.** The old ladder
  quietly re-ran a failed free-tier call on paid DeepSeek. Failures now raise
  and name the explicit paid re-run, so spend is always a decision.

### Added

- **Warm `agy` worker sessions, one-round self-fix, and real token accounting
  (owner decree 2026-07-24).** `call_agy_print()` now calls `agy` with
  `--output-format json`, fixing a real bug: the router recorded zero
  `pin`/`pout`/`cache` and `cost_unknown=True` for every `agy` call, even
  though `agy` genuinely exposes real token usage — cost stays $0 (Google AI
  Pro subscription) but the ledger row is no longer lying about tokens.
  `--session-key <name>` resumes the SAME `agy` `conversation_id` across
  separate `delegate.py` invocations (state in
  `<DATA_DIR>/worker_sessions.json`, never git, pruned after 24h; a stale id
  self-heals with one silent cold retry, never a hard failure). On a verify
  failure, the `agy` channel gets exactly ONE self-fix round — the verify
  command, exit code, and a truncated+redacted tail of its output are sent
  back into the same warm conversation instead of a full context replay —
  opt out with `--no-self-fix`; the ledger records `self_fix_rounds` /
  `self_fix_outcome`. `send_note()`'s filename slug is now ASCII-only
  (`c.isascii() and c.isalnum()`), fixing Persian subjects producing Persian
  filenames that broke the manager surfacing hook.

- **Large-file write guard (owner decree 2026-07-27) — the reason this release
  exists.** On 2026-07-27 a three-line fix delegated to a cheap model came back
  as a *full-file rewrite* that shrank a 50KB source file to 245 lines; only
  git saved it. Full-file replacement is now refused where it is dangerous:
  a `===FILE:` block targeting an existing file ≥ `LARGE_FILE_BYTES` (12 KB) is
  rejected, as is any rewrite shrinking a file below `MAX_SHRINK_RATIO` (50%)
  of its current size. Such edits must use the new `===PATCH:` /`===OLD===` /
  `===NEW===` protocol, which is applied by literal exact match — the `old`
  text must occur **exactly once** (zero matches or two matches are both
  rejected), never a regex or fuzzy match. The escape hatch
  `--allow-full-rewrite` exists on the CLI only and is deliberately not
  exposed on the MCP tool.

- **Pinned Telegram dashboards (`src/dashboards.py`, `dashboard_push`)** — Two
  Telegram messages the owner can glance at instead of asking an agent: an
  unread-inbox digest (grouped by project, via the existing
  `list_notes(..., peek=True)` — rendering a dashboard never marks a note
  read) and an open-tasks digest parsed from `_memory/QUEUE.md`'s
  `## 🎯 ترتیبِ اجرا` table (rows already marked `✅` are skipped). Both are
  sent once, pinned, and thereafter *edited in place*; the render is
  sha256-hashed against the last-pushed content so an unchanged dashboard
  costs zero Telegram API calls, and a deleted/unpinned message is detected
  from Telegram's error and transparently re-sent + re-pinned. Every dynamic
  value is HTML-escaped (`parse_mode=HTML`, not MarkdownV2 — Persian prose and
  repo names routinely contain `<`/`*`) and output is capped at Telegram's
  4096-char limit with a `… +N more` marker. Both dashboards end with a RAG
  ingest freshness line (`RAG ingest: 4h ago (312 rows)`, `⚠️ stale` past 24h,
  `⚠️ never run` if `src/ingest.py` has never completed — `ingest.py` now
  writes `<DATA_DIR>/last_ingest.json` on every successful run). Every
  `send_note()` call also fires a best-effort short Telegram ping and
  refreshes the pinned inbox dashboard (`notify=False` to suppress, e.g. in
  tests) — a Telegram outage never blocks or loses the note itself. New CLI
  flags `--dashboard inbox|tasks|both` (push) and `--dashboard-dry-run
  inbox|tasks|both` (render to stdout, no API call) on `src/delegate.py`, and
  a new MCP tool `dashboard_push` that returns only a short status line,
  never the dashboard body.

### Fixed

- **`delegate_research` never performed live search — grok answered from
  stale training memory for months.** `src/delegate.py` sent grok a plain
  `chat/completions` call with no search tool at all, while `mcp/server.py`
  advertised "default grok = live web/X search". The old live-search request
  field (`search_parameters`) is now HTTP 410 Gone ("Live search is
  deprecated. Please switch to the Agent Tools API"). Grok now routes
  through xAI's `/v1/responses` endpoint with a server-side `web_search`
  tool via the new `call_xai_responses()`; `--no-search` (CLI) / `search`
  (MCP, default true) forces the old cheap plain-chat path when live data
  isn't needed.
  - **Real cost is $0.02–$0.05/call, not $0.003.** `usage.cost_in_usd_ticks`
    (xAI's own billed cost; 1 tick = 1e-10 USD) proved each server-side
    `web_search` call bills a flat **$0.005** on top of tokens, and a
    research question typically triggers 3-6 calls — the token-table
    estimate the router used to publish ($0.003) doesn't see this at all.
    The ledger now records `cost_in_usd_ticks / 1e10` verbatim for xAI
    `/v1/responses` calls (plus `web_search_calls` in the audit row) instead
    of the token-table guess; the table is kept only for the pre-flight
    `--estimate` path. A live 5-question benchmark measured **$0.034/call
    average** for grok-4.3.
  - **Runaway-cost guard: `max_tool_calls` (default 6, `XAI_MAX_TOOL_CALLS`),
    overridable via `--max-tool-calls` / the MCP `max_tool_calls` arg.** A
    live probe found ONE uncapped grok-4.5 research question made 15
    `web_search` calls, pulled 209,956 input tokens, and cost **$0.389** —
    100x a caller's expectation. A warning is logged whenever
    `web_search_calls` hits the cap (the answer may be truncated research).
  - **Added `grok-4.5` as an opt-in model** (`resolve_model("grok-4.5")` /
    `grok45` / `grok4.5`); **`grok` (4.3) stays the default.** A live 5-
    question A/B found grok-4.3 5/5 correct at $0.172 total vs grok-4.5's
    4/5 (it contradicted 4.3 and dropped a requested detail) at $0.618
    total — 3.6x the cost and 2.2x the latency for equal-or-worse quality.
  - **Cost math previously ignored cached-input pricing for grok and any
    long-context surcharge.** `grok`/`grok-4.5` now define `cin_cached`
    (0.20 / 0.30 per 1M) and `cin_long`/`cout_long` + `long_ctx_threshold`
    (200k tokens — the rate doubles above it); the shared
    `compute_token_cost()` helper (used by both the chat and worker paths)
    now applies both, where before a >200k-token grok call silently
    under-reported cost.

- **CI ruff now uses the project's pinned version, not a floating one.** The
  `Test` workflow ran `uvx ruff check`, which fetches the *latest* ruff each
  run, while local checks use `uv run ruff` (the locked `0.15.22`). When ruff
  `0.16.0` shipped with stricter defaults, the first push afterwards
  (`e10e466`) turned CI red with 76 style errors even though the code was
  clean under the pinned version. The step now runs `uv run ruff check`, so CI
  and local use the same locked ruff and stay deterministic.

- **Inter-session messaging (`send_note` / `list_notes`)** — Agents can now send notes to other projects' inboxes via the router (`r note <project> <message>` or the `send_note` MCP tool). Notes are written durably into the target project's vault (`~/.local/share/agent-projects/<project>/workspace/inbox/`) and are redacted for secrets. Unread notes can be read and marked as read using `r inbox` or the `list_notes` MCP tool. A `r inbox --peek` command is available to just show counts, and a new `session_start_inbox.sh` hook runs it on session start. Delivery is strictly turn-boundary (never mid-turn push), avoiding prompt-injection as execution text.

- **agy now writes to the target workdir (`--add-dir`), not its sandbox.**
  Without binding the workdir into agy's (antigravity-cli) workspace, agy
  non-deterministically sandboxed its file writes into
  `~/.gemini/antigravity-cli/scratch/` and reported success, so nothing
  reached the target directory and the router saw `0 files changed`. This is
  the actual cause of the 2026-07-21 "agy did the work but 0 files changed /
  fabricated hash" signal (a live probe found the file in agy's scratch, not
  the cwd). `agent_delegate` now passes `--add-dir <workdir>`; probed on git
  and non-git workdirs, 3/3 runs landed the file in the target. The
  non-git-detection and UNVERIFIED changes below remain as the safety net.
- **`agent_delegate` now detects file changes in a non-git workdir.** Change
  detection ran *only* `git status --porcelain`, so when the workdir was not a
  git repo the diff was always empty and a runner that genuinely wrote files
  was reported as `0 files changed`. This is the real cause of the
  2026-07-21 "agy hallucinated" signal: a live probe confirmed agy (Gemini 3.1
  Pro) wrote the requested file correctly, but the router — measuring an empty
  non-git dir with git — reported zero. A filesystem snapshot (size + mtime,
  `.git` skipped) is now used as a fallback whenever the workdir is not a git
  work tree; git repos keep using `git status` (so `.gitignore` is respected).
- **Agent delegation no longer reports a silent success.** `status` was
  derived purely from the runner's exit code, so a clean exit with no
  filesystem effect was reported as `COMPLETED`. It is now flagged
  `COMPLETED — ⚠️ 0 files changed, UNVERIFIED` when the runner changed no
  files and no `--verify` ran, and `COMPLETED — ⚠️ VERIFY FAILED` when an
  explicit verify failed — a safety net for the case a runner really did
  nothing. Regression tests now exercise the real write outcome of a fake
  runner (git and non-git workdirs), not just its argv.

### Added

- **Telegram file delivery (`send_to_owner`)** — New CLI flag (`--send-to-owner`) and MCP tool to send files directly to the owner's Telegram chat as document attachments. Uses the existing `httpx` dependency and the project's own dedicated bot token (`AI_ROUTER_BOT_TOKEN` + `TELEGRAM_OWNER_CHAT_ID`) from the rule-035 vault (`ai-router/secrets/.env`) — a bot distinct from any other project's. This prevents large deliverables and notes from getting lost in chat.

- **Task-note routing (`route_task`)** — A new routing layer (`route_task` MCP tool and `--route-task` CLI flag) that sits in front of `agent_delegate` and `send_note`. It executes a task-note with strict push/merge refusal and a $0-first executor ladder (defaults to `agy`, falls back to paid `codewhale` with `flash` on verification failure or crash). It optionally reports back the result via `send_note` to the calling project's inbox.

- **Phase 3a+: Sessions Retrieval (`r sessions` / `rules_lookup`)** — semantic
  retrieval over past session context (the `~/.local/share/agent-projects/*/workspace/SESSION.md` files).
  Files are chunked by markdown headings (e.g., `## YYYY-MM-DD` and subheadings) and stored in the
  `session_chunks` pgvector collection. Includes an optional `--reindex` flag for incremental indexing.
  Exposed via the `r sessions` CLI and available to MCP hosts by passing `collection: "sessions"`
  to the `rules_lookup` tool.

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
