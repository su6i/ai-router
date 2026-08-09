# ai-router

[فارسی](docs/fa/README.fa.md) · **[Architecture](docs/ARCHITECTURE.md)**

Cost-accounting LLM gateway: one door to every model, every call tagged,
budgeted and ledgered. Companion infrastructure for multi-agent projects that
need **cost-per-task as a SQL query** instead of a guess. Full design:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## What's here

| Path | What |
| --- | --- |
| `src/delegate.py` | Single LLM gateway (grunt-work delegation) — provider-echoed proof, exact-hash cache, session memory, worker mode (`--files`), audit ledger |
| `src/dashboards.py` | Two pinned, edit-in-place Telegram dashboards (unread inbox notes, open `QUEUE.md` tasks) + a short Telegram ping fired on every `send_note()` |
| `mcp/server.py` | MCP-lite server — exposes `delegate_research`/`delegate_worker` as MCP tools over stdio, so any MCP host can discover cheap delegation without a CLI |
| `tests/` | pytest suite for `src/delegate.py` and `mcp/server.py` |
| `docs/ARCHITECTURE.md` | Full design: Postgres + pgvector schema, exact-hash prompt cache, Prometheus/Grafana observability |
| `docker-compose.yml` | pgvector Postgres + monitoring stack |
| `.env.example` | Reference list of the variables the vault `.env` must define (rule 035 — the repo never holds one) |
| `CHANGELOG.md` | Notable changes, newest first |

`delegate.py` keeps no state in the repo: cache, audit log and session memory
live in the vault (`~/.local/share/agent-projects/ai-router/data/`, override
with `AI_ROUTER_DATA_DIR`); secrets load from `<vault>/secrets/.env` layered
over `_shared/secrets/.env`. The data dir is created with mode `0700`
(owner-only) so the audit ledger and cache stay private on multi-user machines.

Diagnostics (budget notices, fallback warnings, key fingerprint) go to
**stderr** via Python logging — never stdout; pass `--quiet` to suppress
INFO-level lines.

## Usage

### Inter-session Messaging

Agents can send notes to other projects using `r note` or the `send_note` MCP tool. Notes are stored in `<vault>/agent-projects/<project>/workspace/inbox/`.

```bash
# Send a note to the 'arix' project
r note arix "Please review my latest PR."
# Or via CLI
python3 src/delegate.py --note arix -p "Please review my latest PR."

# Read notes for the current project (marks as read)
r inbox
# Or via CLI
python3 src/delegate.py --inbox

# Peek at unread notes without marking them read
r inbox --peek
# Or via CLI
python3 src/delegate.py --inbox --peek
```

**Security Note:** Note contents are untrusted text from other isolated sessions. Delivery is strictly turn-boundary (never mid-turn push, never an interrupt). When viewing `r inbox`, notes are clearly framed as data ("note from <project>: <body>"). They must never be executed as instructions automatically.

### Task-note routing

Execute a task-note from another agent with strict push/merge refusal and a $0-first executor ladder. The router runs `agy` by default; if the run fails or the independent verification step (`--verify`) fails, it automatically falls back to a paid model (`codewhale` with `flash`). It then optionally reports back the result via `send_note` to the calling project's inbox.

```bash
python3 src/delegate.py --route-task <path-to-note-file> --verify "uv run pytest -q"
```

### Telegram Delivery & Dashboards

You can send files directly to the owner's Telegram as documents (attachments) using the `--send-to-owner` CLI flag or MCP tool. This is useful for delivering WO/note files without losing them in chat.

```bash
python3 src/delegate.py --send-to-owner --file /absolute/path/to/file.md --title "Short title"
```

**Pinned dashboards.** Instead of spamming the owner's chat, `src/dashboards.py` maintains two Telegram messages that get **pinned once and edited in place** thereafter — an unread-inbox digest and an open-`QUEUE.md`-tasks digest:

```bash
# Push/refresh both pinned dashboards (edits in place; no-op — zero API calls — if content is unchanged)
python3 src/delegate.py --dashboard both        # or: inbox | tasks

# Preview the rendered dashboard(s) on stdout without touching Telegram at all
python3 src/delegate.py --dashboard-dry-run both
```

Each render groups unread notes by project (via `list_notes(..., peek=True)`, which — critically — never marks a note read just because a dashboard glanced at it) or parses the `## 🎯 ترتیبِ اجرا` table out of `~/.local/share/agent-projects/_memory/QUEUE.md`, skipping rows already marked `✅`. Both dashboards end with an **RAG ingest freshness** line (`RAG ingest: 4h ago (312 rows)`, with a `⚠️ stale` marker past 24h or `⚠️ never run` if `src/ingest.py` has never completed) so nobody trusts a stale semantic index. Output is capped at Telegram's 4096-char limit (oldest/lowest-priority items get truncated with a `… +N more` marker) and every dynamic value is HTML-escaped (`parse_mode=HTML`, not MarkdownV2 — Persian prose and repo names routinely contain `<`/`*`).

Every `send_note()` call (CLI `--note`, MCP `send_note`) also fires a short, best-effort Telegram ping (`🔔 note → <project> · from <project> · <priority>`) and refreshes the pinned inbox dashboard. Pings are **coalesced**: notes that share a sender and subject within 15 minutes edit the first ping in place and append `… and N more` rather than posting again, so one announcement broadcast to seven projects is one message, not seven — a Telegram outage never blocks or loses the note itself (pass `notify=False` to the Python API to suppress this, e.g. in tests). The MCP tool `dashboard_push` (`{"kind": "inbox"|"tasks"|"both"}`) exposes the push side to any MCP host; like every other tool here it returns only a short status line (`"inbox: edited (id=...) · tasks: unchanged"`), never the dashboard body.

Requires `AI_ROUTER_BOT_TOKEN` (the project's own dedicated bot, `@su6i_ai_router_bot`) and `TELEGRAM_OWNER_CHAT_ID` in the rule-035 vault (`ai-router/secrets/.env`). This is a distinct bot from any other project's Telegram bot.

### One-shot chat

```bash
python3 src/delegate.py --model flash -p "summarize this changelog"
```

`--model` accepts an alias (default `minimax`; also `agy`, `flash`, `pro`, `grok`,
or a full model name — see `ALIASES` in
`src/delegate.py`). `--plan <file>` reads the prompt from a file instead of
`-p`; `--out <file>` writes the answer to a file instead of stdout.

Every id `agy models` reports is routable by that exact name, at $0 on the Google
AI Pro subscription — including `claude-sonnet-4-6` and `claude-opus-4-6-thinking`,
which draw on a **different** quota pool from the Gemini ids and so can absorb work
without competing with the default worker. Pass the id exactly as printed and never
add `--effort`: the effort level is already part of the id, and agy rejects the pair
outright for the Claude models. `agy` remains an alias for `gemini-3.1-pro-high`.

### Sessions

```bash
python3 src/delegate.py --model flash --session refactor-foo \
  -p "list the functions in src/foo.py that need docstrings"
python3 src/delegate.py --model flash --session refactor-foo \
  -p "now write docstrings for the ones you listed"
```

`--session <name>` remembers the conversation across calls, keyed by name, in
the vault (never in the repo). `--new` resets a named session before running.
`--system <text>` sets a persona/system instruction.

### Rules retrieval: r rules

`ai-router` provides semantic retrieval over rule files (the `.agent/constitution/rules/*.md` directory, `docs/**/*.md`, and `CLAUDE.md`).
Translations (`docs/fa/`, `*.fa.md`) are excluded from the index: they duplicate the canonical English content and drown cross-lingual queries — the multilingual embedder still matches Persian queries against the English chunks.
This uses a local ONNX model (`intfloat/multilingual-e5-small`) and pgvector to find relevant rule chunks instead of loading whole files into context:

```bash
# Query the index (returns top 5 chunks by default)
r rules "قانون کامیت"

# Re-index all markdown files (only embeds changed chunks)
r rules --reindex
```

The output is hard-capped at ~8000 characters to protect context limits.
If the index was built on a different commit than the current one, `r rules` will print a single warning line before the results.

### Sessions retrieval: r sessions

`ai-router` provides semantic retrieval over past session context (the `~/.local/share/agent-projects/*/workspace/SESSION.md` files) exactly like it does for rules.
It chunks by headings (like `## YYYY-MM-DD`) and stores them in the `session_chunks` pgvector collection.

```bash
# Query the sessions index (returns top 5 chunks by default)
r sessions "codewhale token accounting"

# Re-index all sessions
r sessions --reindex
```

This is exposed to MCP hosts via the `rules_lookup` tool by passing `collection: "sessions"`.

### Code retrieval: r code

Phase 3b indexes **code** the way `r rules` indexes text: git-tracked
`*.py`/`*.sh` files are chunked at function/class/method boundaries
(tree-sitter AST), embedded with the same local e5-small model, and stored in
pgvector next to a static call graph. Full design and honest economics:
[`docs/CODE-RAG.md`](docs/CODE-RAG.md).

```bash
# Query: chunks with path:start-end refs, output capped ~2k tokens
r code "where is the budget cap checked" -k 5

# --graph adds 1-hop callers/callees of each hit
r code "budget cap abort" --graph

# Incremental reindex (only files changed since the indexed commit)
r code --reindex

# Full rebuild
r code --rebuild
```

A one-line stale-index warning is printed when the index commit differs from
`HEAD`. The same retrieval is exposed to MCP hosts as the `code_lookup` tool
("use this instead of exploratory file reads").

### RAG index & auto-ingest

`src/rag_ingest.py` unifies semantic indexing for rules, sessions, and code into a single process. It is incremental by default (skipping unchanged content via fast hashing) and tracks freshness via a state file (`rag_state.json`). You can interact with the ingest CLI directly:

```bash
# Ingest all collections (incremental)
uv run src/rag_ingest.py --collection all

# Force a full rebuild for sessions, outputting JSON status
uv run src/rag_ingest.py --collection sessions --force --json

# Ingest a single file and print a mechanically provable receipt derived from stored DB rows
uv run src/rag_ingest.py --receipt ~/.local/share/agent-projects/ai-router/workspace/SESSION.md

# Check current freshness status
uv run src/rag_ingest.py --status
```

The `sessions` collection covers per-repo `SESSION.md`, `_memory/sessions/*.md`, `_memory/handoffs/*` (markdown/text notes), and per-repo archived session digests under `<repo>/workspace/archive*`. Incremental hashing ensures unchanged files are skipped in ~0.1s, and deleted files have their chunks removed automatically.
The freshness of the RAG index is reported at the bottom of the 📋 open-tasks Telegram dashboard.

### Cache

Identical one-shot calls (same model + system + prompt + max_output_tokens) hit the exact-hash
cache automatically — the repeat costs $0 and never touches the provider.
Text is NFC-normalized before hashing.

The cache enforces a max of 5000 rows and 90 days retention, pruning silently on inserts. You can also manually trigger pruning:
```bash
python3 src/delegate.py --cache-prune
```

`--session` calls are never cached (a multi-turn conversation isn't safe to
serve from a single cached turn). Force a live call with `--no-cache`:

```bash
python3 src/delegate.py --model flash -p "same prompt as before"            # cache HIT, $0
python3 src/delegate.py --model flash -p "same prompt as before" --no-cache  # forces a real call
```

### Provider prompt caches

Many API providers (like DeepSeek, Gemini, and MiniMax) automatically cache prompts based on exact prefix matching. `delegate.py` accounts for this discount automatically:

- Cash savings are explicitly reflected in the printed cost.
- Cache hit rates (e.g., `cache hit rate: 85.0%`) are displayed in the worker summary and `r cost` reports.
- Worker mode uses prefix discipline (files first, task last) to maximize prefix cache efficiency.

### Worker mode

`delegate.py --files` hands a cheap model direct read/write access to files on
disk instead of returning code as chat text — the generated code never enters
the caller's context, only a short summary does:

```bash
python3 src/delegate.py --model flash \
  --files "src/foo.py,tests/test_foo.py" \
  --allow-write "src/**,tests/**" \
  --verify "uv run pytest -q" \
  --retries 1 \
  -p "add a docstring to foo()"
```

- `--files` — comma-separated files the worker reads and may rewrite.
- `--allow-write` — comma-separated globs (relative to cwd) gating every
  write; no flag means no writes.
- `--verify` — caller-supplied shell command run after writing (never
  guessed).
- `--retries` — verify-failure retries (default 1, max 2); the worker gets
  the verify output back and one more attempt per retry.
- `--session-key <key>` — `agy` channel only: resume the SAME `agy`
  conversation across separate `delegate.py` invocations instead of cold
  starting every call. See "Warm agy sessions & self-fix loop" below.
- `--worker-sessions` / `--worker-sessions-clear [key]` — list, or clear
  (one key or everything), the warm-session state file.
- `--no-self-fix` — disable the one-round agy self-fix retry on verify
  failure (default: enabled).

Output — the only thing that reaches the caller's context:

```
files written : src/foo.py (312B)
rejected      : (none)
verify        : uv run pytest -q → PASS (1.2s)   [attempt 1/2]
worker summary: added a one-line docstring to foo()
cost          : $0.000421 · model echoed: deepseek-v4-flash
```

Full wire protocol: the private `DELEGATE-TOOL-DESIGN.md` (vault).

### Warm agy sessions & self-fix loop

`call_agy_print()` invokes `agy` with `--output-format json`, which returns a
stable `conversation_id` plus REAL `usage.input_tokens` /
`usage.output_tokens` / `usage.cache_read_tokens`. The router used to record
zeros here and mark the cost "unknown" — that was a bug in how the router
called `agy`, not a real limitation of the `agy` CLI. Cost stays $0 (Google
AI Pro subscription — never billed) but the ledger now carries true token
counts, and `cost_unknown` is no longer set for the `agy` channel.

Pass `--session-key <name>` to resume the SAME `agy` conversation across
separate CLI invocations instead of cold-starting every call — agy reuses
its own server-side context for that conversation, which shows up as
non-zero `usage.cache_read_tokens` on the resumed call. State is a small
JSON file in the vault data dir
(`~/.local/share/agent-projects/ai-router/data/worker_sessions.json`, never
the repo), keyed by the session key, pruned after 24h. If the stored
conversation id has expired or gone stale, the router drops it and retries
once, cold — a dead id never hard-fails a delegation.

On a verify FAILURE (`--verify`), the `agy` channel gets ONE self-fix round
before giving up: the verify command, its exit code, and the last ~4000
(redacted) chars of its output are sent back into the SAME warm
conversation — not the whole task re-flattened from scratch — so the model
pays for the delta, not a full context re-read. Exactly one round — the reviewer steps in only after a second failure; a
second failure returns to the caller with a structured report, never a
silent extra retry. Disable with `--no-self-fix`. The audit ledger records
`self_fix_rounds` (`0` or `1`) and `self_fix_outcome` (`fixed` / `failed` /
`skipped`).

### Large File Guard

A full-file rewrite of an existing file >= 12KB (`LARGE_FILE_BYTES`) is rejected, as is any rewrite shrinking a file below 50% (`MAX_SHRINK_RATIO`) of its current size. Such edits must use the `===PATCH:` / `===OLD===` / `===NEW===` protocol, applied by literal exact match where the old text must occur exactly once. CLI escape hatch `--allow-full-rewrite`; not exposed on MCP.

### Work-order guard: `scripts/wo_guard.sh`

Prompt rules are advice a model can ignore; this runs inside `--verify`, so
breaking one fails the delegation instead of being reported as success. Put it
first in the verify chain:

```bash
scripts/wo_guard.sh --repo /abs/path/to/repo --branch fix/the-task --base <sha> \
  --once 'src/mod.py:^SENTINEL =:1' \
  --then 'uv run --directory /abs/path/to/repo pytest -q'
```

It refuses to pass when HEAD is not the branch the task named, when that branch
does not descend from `--base` (a stale cut silently reverts whatever landed in
between), when conflict markers or unresolved paths remain, when a `--once`
pattern does not occur exactly the stated number of times — the cheap check for
a patch applied twice — or when any changed file lost ZWNJ (U+200C, نیم‌فاصله)
characters versus `--base` (`scripts/zwnj_guard.py`, run automatically, no flag
needed) — the cheap check for a worker silently mangling Persian half-spaces
while editing a file (real incident: `fix/du-naming-and-interim-track`,
ApplyForge, restored by hand). On success it prints a `===WO-GUARD-RECEIPT===`
block of measured facts (branch, base, head, commit count, files changed,
verify exit), preceded by a `===ZWNJ-GUARD-RECEIPT===` block of per-file ZWNJ
counts. Treat the receipts as the numbers, and anything the model narrates that
contradicts them as fabrication.

### Worker context discipline

To prevent workers from churning through unnecessary tokens or getting lost in huge files, `delegate.py` strictly enforces context hygiene:

1. **Repo Map**: A compact (`< 4000` chars) repository map of top-level symbols is automatically generated by `src/repo_map.py` and prepended to all worker/agent prompts.
2. **Channel System Prompts**: Channel-specific templates (`templates/system-prompts/*.md`) are dynamically injected at the very top of worker prompts to establish the correct persona and capability rules for the delegated model.
3. **Preamble Injection**: A constant 5-line preamble of strict reading rules is injected at the start of every prompt. Placing it *before* the variable file content maximizes API provider prefix-cache hits.
4. **Template Rules**: The `AGENTS-context-discipline.md` template defines the full ruleset (e.g., read a file once, use `grep -n`, batch related reads, don't paste large files back).

### Audit

```bash
python3 src/delegate.py --audit
```

Prints `audit.log` (one JSON line per call: model asked/echoed, session,
project, commit, cost, cached; worker-mode calls add files written/rejected,
verify command/status, attempts).

### Channel Registry

`delegate.py` routes tasks to execution channels (e.g. `agy`, `codewhale`, `codex`, `copilot`). Channel availability is managed by a local registry.
Channels can be enabled/disabled by the `channels.json` file in the data dir (`~/.local/share/agent-projects/ai-router/data/channels.json`) or overridden by the `AI_ROUTER_DISABLE_CHANNELS` environment variable (e.g., `AI_ROUTER_DISABLE_CHANNELS=agy,copilot`).

- `r channels` (or `--channels`) prints an autodetected table showing the status, CLI binary presence, and auth state of all known channels.
- `--enable <channel>` / `--disable <channel>` modifies the `channels.json` registry file.

Model ladder: the default worker channel stays `agy` (Gemini 3.1 Pro, Google AI Pro subscription). The `copilot` runner defaults to `gpt-5-mini`, which has a **0× premium-request multiplier** on Copilot Pro — it never consumes the 300 premium requests/month. Escalate harder tasks explicitly with `--model gpt-5` or `--model claude-sonnet-4.5`; those calls are counted against `copilot_premium_requests_month`.

Premium-request multipliers are **not hardcoded**: they live in `copilot_multipliers.json` in the data dir (seeded on first copilot call), because GitHub changes rates without notice and exposes **no API** for them (personal-plan `seat_info`/usage endpoints are org-only and 404; the internal token exchange rejects CLI tokens — live-checked 2026-07-19). Unknown models bill at the file's `default` (1×) — a model rename can never silently look free. Each call's multiplier is logged as `premium_requests`. As an independent check, `r cost` also queries GitHub's billing API for the **Copilot overage actually billed this month** (requires `gh auth refresh -h github.com -s user` once): within the monthly quota this is `$0`, and a non-zero value means the premium quota was exceeded and real money is being spent — the cue to reconcile `copilot_multipliers.json`.

### Budgets

Budget caps fail loudly — a job over its cap aborts; silent overspend is
forbidden. Limits are configured in `<vault>/data/budgets.json`. If no file
exists, spend is uncapped but a warning is printed to stderr.

Schema (see `budgets.example.json` in the repo root):

```json
{
  "monthly_usd": 5.0,
  "weekly_usd": 2.0,
  "per_session_usd": 0.50,
  "per_project_monthly_usd": {},
  "daily_calls": {"google-ai-pro": 50}
}
```

`daily_calls` caps delegated calls per `quota_channel` per calendar day —
subscription/free channels always report `cost_usd=0`, so USD caps never brake
them; their scarce unit is daily quota. Over the cap aborts loudly; at ≥80% a
warning is printed. A missing key means uncapped. Cache hits don't count.

Use `--estimate` to dry-run a call: prints estimated tokens, cost, current
budget usage, and today's per-channel call counts without calling the provider
or writing to the audit log. `--cost` appends the same per-channel counts.

### Cost Report

```bash
python3 src/delegate.py --cost --by model
```

Aggregates `audit.log` into an aligned text table of spend and cache hit rates.

- `--cost` — totals for all time
- `--since YYYY-MM-DD` or `--today` — time filtering
- `--by <field>` — group by `model` (default), `project`, `session`, `via`, or `day`

### Shell wrapper: `r()`

Source `shell/r.sh` once from your shell rc (bash or zsh):

```bash
echo 'source /Users/su6i/@-github/ai-router/shell/r.sh' >> ~/.zshrc
```

Then delegate from any directory without touching an agent's context:

```bash
r flash "write a regex that matches ISO-8601 dates"   # chat (words → one -p)
r agy --files src/calc.py --allow-write "src/**" --verify "pytest -q" -p "fix the bug"
r cost --today                                        # print today's cost report
r audit                                               # print the ledger
```

The first argument is always the model (unknown names fail loudly with the
alias list). If the second argument starts with `-`, everything is passed to
`delegate.py` unchanged, so every flag works. Overrides: `AI_ROUTER_REPO`,
`AI_ROUTER_PYTHON`.

### MCP server

`mcp/server.py` exposes the same `delegate.py` (same ledger, cache, caps,
secrets path) as three MCP tools, so any MCP host — Claude Code first — can
discover and use cheap delegation mid-task without anyone remembering to ask.

| Door | Best For | Default Model | Notes |
| --- | --- | --- | --- |
| **`delegate_research`** | Fact lookup, live-data checks, doc verification | `agy` (Gemini 3.1 Pro, grounded search, **$0**) | Paid `grok` must now be named explicitly (~$0.02–$0.05/call). |
| **`delegate_worker`** | Known files: mechanical changes, tests, boilerplate | `agy` (free, Google AI Pro sub) | Pass known file paths. Generated code never crosses the wire. |
| **`delegate_agent`** | Unknown files: multi-step find+fix, exploration | `agy` (Gemini Pro) | Wraps `agy` headless or `codewhale exec`. Returns a short summary. |
| **`send_to_owner`** | Delivery of files directly to owner's Telegram | N/A | Bypasses context limits, useful for final WO deliverables. |
| **`dashboard_push`** | Refresh the two pinned Telegram dashboards | N/A | Edits in place; returns a short status line only, never the dashboard body. |

Register it once, user scope, so it's available in every project:

```bash
claude mcp add --scope user ai-router -- python3 /Users/su6i/@-github/ai-router/mcp/server.py
```

Three tools only, all capped — no uncapped chat tool, ever:

- **`delegate_research`** — fact lookup / live-data checks / doc
  verification. **The default is `agy` (Gemini 3.1 Pro on the Google AI Pro
  subscription): $0, using agy's own grounded web search**. Note the
  implementation constraint: the agy branch
  routes through `agent_delegate()`, *not* `delegate()`, because the latter
  appends `AGY_NO_TOOLS_ADDENDUM` and would leave agy answering live-fact
  questions from memory with no signal that it never searched. `max_output_tokens`
  and `max_tool_calls` apply to `grok` only.

  The paid path below must now be requested explicitly with `model="grok"`
  (4.3, via xAI's `/v1/responses`
  endpoint with the server-side `web_search` tool — the old
  `chat/completions` live-search field is HTTP 410 Gone as of 2026-07). Each
  `web_search` call adds a flat **$0.005** on top of tokens and a question
  typically triggers 3–6, so a real research call costs **~$0.02–$0.05, not
  $0.003** — `usage.cost_in_usd_ticks` (xAI's own billed cost) is recorded
  verbatim in the ledger, not the token-table estimate that can't see
  per-search-call billing. `search` (default true) turns the tool off for
  the cheap plain-chat path; `max_tool_calls` (default 6) caps `web_search`
  calls per request — one uncapped live question made 15 calls and cost
  $0.389. `grok-4.5` is available as an opt-in, but when you do pay for grok
  the version to ask for stays 4.3: a live 5-question A/B had 4.3 5/5 correct at $0.172 vs 4.5's 4/5 at
  $0.618 (3.6x the cost, equal-or-worse quality). Answer is capped by
  `max_output_tokens` (default 500, max 2000) — a low default, not a promise.
- **`delegate_worker`** — grunt coding work (default model `agy`). Same
  contract as CLI worker mode: `files`/`allow_write`/`verify`/`retries`
  mirror `--files`/`--allow-write`/`--verify`/`--retries`; `workdir` (an
  absolute path) is required because the MCP server process does not
  inherit the caller's cwd. Returns only the existing ≤25-line summary —
  generated code never crosses the wire.
- **`delegate_agent`** — multi-step grunt tasks needing exploration (find+fix
  across unknown files, iterative debugging). Wraps `agy` (default) or `codewhale`
  behind our budgets. Returns only a ≤25-line summary of files changed, verify
  result, and cost. Prefer `delegate_worker` when the file list is known.
  Router-managed headless `agy` launches pass `--dangerously-skip-permissions`:
  since agy 1.1.3, `--mode accept-edits` no longer auto-approves
  `write_file`/`command` in print mode, so every headless run died with
  "permission check failed … auto-denied". The flag applies only to these
  managed launches (workdir-confined task, output to a log), never to
  interactive sessions. They also pass `--add-dir <workdir>`: without it agy
  non-deterministically writes into its own sandbox
  (`~/.gemini/antigravity-cli/scratch/`) instead of the target dir, so the
  file never lands and the router reports `0 files changed` (the false "agy
  did nothing" signal). Change detection uses `git status` in a git workdir
  and a filesystem snapshot otherwise; a run that exits 0 but changes 0 files
  with no `--verify` is reported as `COMPLETED — ⚠️ UNVERIFIED` (and
  `⚠️ VERIFY FAILED` when an explicit verify fails) rather than a trustworthy
  success — always pass `verify` on change-work.

Claude models stay banned inside delegate (unchanged). Audit rows from MCP
calls get `via: "mcp"` (an extra field alongside the existing columns) so
cost-per-door is a query; `r()`/CLI rows stay as-is (the field is absent,
not null). Transport: stdio only, local machine, no HTTP/SSE, no auth (v1
non-goal).

### Delegation triggers (making the architect actually call the tools)

Tools that merely exist don't get called — the premium architect model
defaults to writing code itself. Two layers push it toward the worker:

- **Imperative tool descriptions** — both MCP descriptions say *when to use
  the tool instead of* Edit/Write or WebSearch (implementation over ~40
  lines, test files, mechanical multi-file changes; live facts / doc
  checks), plus the golden rule: decide **before** reading the target files
  — pass paths, not contents.
- **`hooks/delegate_nudge.py`** — a PreToolUse hook (registered globally in
  `~/.claude/settings.json`, matcher `Write|Edit`) that denies the *first*
  large code write (> 40 new lines, code suffixes only; docs, config and
  scratchpad files exempt) with a reminder to call `delegate_worker`. A
  second attempt on the same file in the same session passes — the
  deliberate escape hatch for architecture-critical code. Fail-open: any
  hook error allows the write.
- **`hooks/worker_channel_nudge.py`** — a PreToolUse hook (matcher `Bash|Command`)
  that blocks direct bash execution of headless workers (`agy print`, `codewhale`),
  redirecting to `delegate_agent` to enforce budget constraints and accounting. A
  deliberate second attempt acts as an escape hatch.
- **`hooks/layer_guard.py`** — a PreToolUse hook (matcher
  `mcp__ai-router__delegate_worker|mcp__ai-router__delegate_agent`) that denies
  the delegation tools to a top-level premium session. Implementation is
  dispatched and reviewed by an intermediate agent, so the worker's output never
  enters the expensive session's context and a neutral reviewer sees the diff
  before the architect does. Unlike the two nudges above there is **no
  second-attempt escape hatch** — the layer boundary is a topology invariant, not
  a per-call judgement. The intermediate layer passes with `SU6I_LAYER2=1` in its
  environment. Every decision is appended to `layer-guard.jsonl` in the vault log
  directory. Fail-open: any hook error allows the call.

## Models

From `MODELS` in `src/delegate.py` (cost per 1M tokens):

| `--model` | API model | Provider | Cost in / out | Role |
| --- | --- | --- | --- | --- |
| `minimax` | `MiniMax-M3` | MiniMax | $0.30 / $1.20 | Default — one-time prepaid credit, spend first |
| `flash` | `deepseek-v4-flash` | DeepSeek | $0.14 / $0.28 | General grunt work — implementation, refactor, tests, boilerplate |
| `pro` | `deepseek-v4-pro` | DeepSeek | $0.435 / $0.87 | Reasoner — escalation target when `flash` fails or needs deeper reasoning |
| `grok` | `grok-4.3` | xAI | $1.25 / $2.50 (+ $0.20 cached in) | Second opinion / current-events knowledge — PAID `delegate_research` escape hatch, explicit only (default is `agy`), not for routine work |
| `grok-4.5` | `grok-4.5` | xAI | $2.00 / $6.00 (+ $0.30 cached in) | Opt-in only — a live A/B found 3.6x the cost of `grok` for equal-or-worse research quality |
| `agy` | `Gemini 3.1 Pro (High)` | Google AI Pro sub (local `agy`) | $0 / $0 | Default coding worker — mechanical changes, tests, boilerplate |

Priority order and full routing rationale (MiniMax credit-exhaustion
fallback, why Claude is never in this router, provider vs. subscription-CLI
distinction): `STRATEGY.md` and `ROLES.md` in
`~/.local/share/agent-projects/_router/` (vault, not in this repo).

### Resilience & Fallbacks

- **Retries**: All provider calls automatically retry on transient errors (HTTP 429, 5xx, or timeouts) with an exponential backoff (1s, then 3s). Hard errors (like HTTP 400 or malformed JSON responses) fail immediately with a specific `ProviderError`.
- **MiniMax Fallback**: If the prepaid `minimax` model fails with a credit exhaustion or 401/402/429 error after retries, the router will automatically fall back to `flash` (`deepseek-v4-flash`).
- **HTTP 503**: A 503 means the provider itself is down (e.g. a paid provider endpoint under load). The built-in 3-attempt retry already ran; there is NO automatic paid fallback for 503 — per the $0-first policy a transient upstream outage does not authorize paid spend. Wait and retry later, or ask the owner before switching channels.

### Secret hygiene

- API keys never travel in URLs (e.g., passed via headers like `x-api-key`), and every error message the MCP server sends over the wire is scrubbed (`key=` query params and any loaded key values are redacted).
- **Stale server caveat**: MCP server processes are long-lived — a session started before a router update keeps running the OLD code until that session restarts. After a router merge, restart open agent sessions (or `/mcp` reconnect) to pick up fixes.

## Status

Infrastructure scaffold — schema and services are being built incrementally.
See `docs/ARCHITECTURE.md` for the phased plan.

## Setup (Data Plane)

1. Put the Postgres credentials in your vault secrets
   (`~/.local/share/agent-projects/ai-router/secrets/.env`) — **not** in the repo.
   `docker-compose.yml` reads that file directly (override the location with
   `AI_ROUTER_ENV_FILE`), so there is deliberately no `cp .env.example .env` step:
   a repo-local `.env` is what rule 035 forbids, and demanding one is what kept the
   data plane down — and every RAG collection stale — from 2026-07-28 to 2026-08-04.
   ```ini
   POSTGRES_USER=airouter
   POSTGRES_PASSWORD=change-me
   POSTGRES_DB=airouter
   POSTGRES_DSN=postgresql://airouter:change-me@localhost:5432/airouter
   ```

2. Start Postgres:
   ```bash
   docker compose up -d
   ```
   Requires Docker (tested with Colima on macOS — `colima start` first if the
   daemon is down). The `usage` schema is applied automatically on first run.
   Confirm it actually came up before moving on; a failed start here is the
   single most common cause of a silently stale index:
   ```bash
   docker ps --filter name=ai-router-db --format "{{.Names}}  {{.Status}}"
   ```

3. Ingest your existing audit log into Postgres:
   ```bash
   uv run src/ingest.py
   ```

## Testing

```bash
cd /Users/su6i/@-github/ai-router
uv sync --group dev
uv run pytest -q
```

Expected: `73 passed` (all suites under `tests/` — offline, no API keys
or vault needed).
