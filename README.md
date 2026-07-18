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
| `mcp/server.py` | MCP-lite server — exposes `delegate_research`/`delegate_worker` as MCP tools over stdio, so any MCP host can discover cheap delegation without a CLI |
| `tests/` | pytest suite for `src/delegate.py` and `mcp/server.py` |
| `docs/ARCHITECTURE.md` | Full design: Postgres + pgvector schema, exact-hash prompt cache, Prometheus/Grafana observability |
| `docker-compose.yml` | pgvector Postgres + monitoring stack |
| `.env.example` | Required environment variables (copy, fill, keep out of git) |
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

### One-shot chat

```bash
python3 src/delegate.py --model flash -p "summarize this changelog"
```

`--model` accepts an alias (default `minimax`; also `flash`, `pro`, `grok`,
`gemini`, `gemini-lite`, `gemma`, or a full model name — see `ALIASES` in
`src/delegate.py`). `--plan <file>` reads the prompt from a file instead of
`-p`; `--out <file>` writes the answer to a file instead of stdout.

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

Output — the only thing that reaches the caller's context:

```
files written : src/foo.py (312B)
rejected      : (none)
verify        : uv run pytest -q → PASS (1.2s)   [attempt 1/2]
worker summary: added a one-line docstring to foo()
cost          : $0.000421 · model echoed: deepseek-v4-flash
```

Full wire protocol: the private `DELEGATE-TOOL-DESIGN.md` (vault).

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
  "daily_calls": {"google-ai-pro": 50, "gemini-free": 400}
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
r gemini --files src/calc.py --allow-write "src/**" --verify "pytest -q" -p "fix the bug"
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
| **`delegate_research`** | Fact lookup, live-data checks, doc verification | `grok` (web search) | Answer is capped by `max_output_tokens`. |
| **`delegate_worker`** | Known files: mechanical changes, tests, boilerplate | `gemini` (free) | Pass known file paths. Generated code never crosses the wire. |
| **`delegate_agent`** | Unknown files: multi-step find+fix, exploration | `agy` (Gemini Pro) | Wraps `agy` headless or `codewhale exec`. Returns a short summary. |

Register it once, user scope, so it's available in every project:

```bash
claude mcp add --scope user ai-router -- python3 /Users/su6i/@-github/ai-router/mcp/server.py
```

Three tools only, all capped — no uncapped chat tool, ever:

- **`delegate_research`** — fact lookup / live-data checks / doc
  verification (default model `grok` = live web/X search). Answer is capped
  by `max_output_tokens` (default 500, max 2000) — a low default, not a
  promise.
- **`delegate_worker`** — grunt coding work (default model `gemini`). Same
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
  interactive sessions.

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

## Models

From `MODELS` in `src/delegate.py` (cost per 1M tokens):

| `--model` | API model | Provider | Cost in / out | Role |
| --- | --- | --- | --- | --- |
| `minimax` | `MiniMax-M3` | MiniMax | $0.30 / $1.20 | Default — one-time prepaid credit, spend first |
| `flash` | `deepseek-v4-flash` | DeepSeek | $0.14 / $0.28 | General grunt work — implementation, refactor, tests, boilerplate |
| `pro` | `deepseek-v4-pro` | DeepSeek | $0.435 / $0.87 | Reasoner — escalation target when `flash` fails or needs deeper reasoning |
| `grok` | `grok-4.3` | xAI | $1.25 / $2.50 | Second opinion / current-events knowledge — not for routine work |
| `gemini` | `gemini-2.5-flash` | Google (free tier) | $0 / $0 | Free-tier grunt work — commit messages, format conversion, categorization |
| `gemini-lite` | `gemini-2.5-flash-lite` | Google (free tier) | $0 / $0 | Free-tier, lighter/faster variant of `gemini` |
| `gemma` | `gemma-4-31b-it` | Google (free tier) | $0 / $0 | Free-tier, open-weight model |

Priority order and full routing rationale (MiniMax credit-exhaustion
fallback, why Claude is never in this router, provider vs. subscription-CLI
distinction): `STRATEGY.md` and `ROLES.md` in
`~/.local/share/agent-projects/_router/` (vault, not in this repo).

### Resilience & Fallbacks

- **Retries**: All provider calls automatically retry on transient errors (HTTP 429, 5xx, or timeouts) with an exponential backoff (1s, then 3s). Hard errors (like HTTP 400 or malformed JSON responses) fail immediately with a specific `ProviderError`.
- **MiniMax Fallback**: If the prepaid `minimax` model fails with a credit exhaustion or 401/402/429 error after retries, the router will automatically fall back to `flash` (`deepseek-v4-flash`).
- **Gemini Fallback**: If `gemini` exhausts the free tier rate limits (HTTP 429) after retries, the router will automatically fall back to `flash`. A loud warning is printed because the fallback incurs non-zero costs.
- **HTTP 503**: A 503 means the provider itself is down (e.g. Google's free gemini endpoint under load). The built-in 3-attempt retry already ran; there is NO automatic paid fallback for 503 — per the $0-first policy a transient upstream outage does not authorize paid spend. Wait and retry later, or ask the owner before switching channels.

### Secret hygiene

- API keys never travel in URLs (gemini uses the `x-goog-api-key` header), and every error message the MCP server sends over the wire is scrubbed (`key=` query params and any loaded key values are redacted).
- **Stale server caveat**: MCP server processes are long-lived — a session started before a router update keeps running the OLD code until that session restarts. After a router merge, restart open agent sessions (or `/mcp` reconnect) to pick up fixes.

## Status

Infrastructure scaffold — schema and services are being built incrementally.
See `docs/ARCHITECTURE.md` for the phased plan.

## Setup (Data Plane)

1. Start Postgres:
   ```bash
   cp .env.example .env
   docker compose up -d
   ```
   Requires Docker (tested with Colima on macOS). The `usage` schema will be applied automatically on first run.

2. Set the database connection in your vault secrets (`~/.local/share/agent-projects/ai-router/secrets/.env`):
   ```ini
   POSTGRES_DSN=postgresql://airouter:change-me@localhost:5432/airouter
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
