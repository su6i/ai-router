# ai-router

[فارسی](README.fa.md) · **[Architecture](docs/ARCHITECTURE.md)**

Cost-accounting LLM gateway: one door to every model, every call tagged,
budgeted and ledgered. Companion infrastructure for multi-agent projects that
need **cost-per-task as a SQL query** instead of a guess. Full design:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## What's here

| Path | What |
| --- | --- |
| `src/delegate.py` | Single LLM gateway (grunt-work delegation) — provider-echoed proof, exact-hash cache, session memory, worker mode (`--files`), audit ledger |
| `tests/` | pytest suite for `src/delegate.py` |
| `docs/ARCHITECTURE.md` | Full design: Postgres + pgvector schema, exact-hash prompt cache, Prometheus/Grafana observability |
| `docker-compose.yml` | pgvector Postgres + monitoring stack |
| `.env.example` | Required environment variables (copy, fill, keep out of git) |
| `CHANGELOG.md` | Notable changes, newest first |

`delegate.py` keeps no state in the repo: cache, audit log and session memory
live in the vault (`~/.local/share/agent-projects/ai-router/data/`, override
with `AI_ROUTER_DATA_DIR`); secrets load from `<vault>/secrets/.env` layered
over `_shared/secrets/.env`.

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

### Cache

Identical one-shot calls (same model + system + prompt) hit the exact-hash
cache automatically — the repeat costs $0 and never touches the provider.
`--session` calls are never cached (a multi-turn conversation isn't safe to
serve from a single cached turn). Force a live call with `--no-cache`:

```bash
python3 src/delegate.py --model flash -p "same prompt as before"            # cache HIT, $0
python3 src/delegate.py --model flash -p "same prompt as before" --no-cache  # forces a real call
```

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

### Audit

```bash
python3 src/delegate.py --audit
```

Prints `audit.log` (one JSON line per call: model asked/echoed, session,
project, commit, cost, cached; worker-mode calls add files written/rejected,
verify command/status, attempts).

### Shell wrapper: `r()`

Source `shell/r.sh` once from your shell rc (bash or zsh):

```bash
echo 'source /Users/su6i/@-github/ai-router/shell/r.sh' >> ~/.zshrc
```

Then delegate from any directory without touching an agent's context:

```bash
r flash "write a regex that matches ISO-8601 dates"   # chat (words → one -p)
r gemini --files src/calc.py --allow-write "src/**" --verify "pytest -q" -p "fix the bug"
r audit                                               # print the ledger
```

The first argument is always the model (unknown names fail loudly with the
alias list). If the second argument starts with `-`, everything is passed to
`delegate.py` unchanged, so every flag works. Overrides: `AI_ROUTER_REPO`,
`AI_ROUTER_PYTHON`.

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

## Status

Infrastructure scaffold — schema and services are being built incrementally.
See `docs/ARCHITECTURE.md` for the phased plan.

## Setup

```bash
cp .env.example .env
docker compose up -d
```

Requires Docker (tested with Colima on macOS).

## Testing

```bash
cd /Users/su6i/@-github/ai-router
uv run --with pytest --with httpx pytest
```

Expected: `28 passed` (`tests/test_delegate_cache.py` +
`tests/test_delegate_worker.py`).
