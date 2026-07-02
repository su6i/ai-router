# ai-router

[فارسی](README.fa.md)

Cost-accounting LLM gateway: one door to every model, every call tagged,
budgeted and ledgered. Companion infrastructure for multi-agent projects that
need **cost-per-task as a SQL query** instead of a guess.

## What's here

| Path | What |
| --- | --- |
| `src/delegate.py` | Single LLM gateway (grunt-work delegation) — provider-echoed proof, exact-hash cache, session memory, audit ledger |
| `tests/` | pytest suite for `src/delegate.py` |
| `docs/ARCHITECTURE.md` | Full design: Postgres + pgvector schema, exact-hash prompt cache, Prometheus/Grafana observability |
| `docker-compose.yml` | pgvector Postgres + monitoring stack |
| `.env.example` | Required environment variables (copy, fill, keep out of git) |

`delegate.py` keeps no state in the repo: cache, audit log and session memory
live in the vault (`~/.local/share/agent-projects/ai-router/data/`, override
with `AI_ROUTER_DATA_DIR`); secrets load from `<vault>/secrets/.env` layered
over `_shared/secrets/.env`. Run the tests with:

```bash
uv run --with pytest --with httpx pytest
```

## Status

Infrastructure scaffold — schema and services are being built incrementally.
See `docs/ARCHITECTURE.md` for the phased plan.

## Setup

```bash
cp .env.example .env
docker compose up -d
```

Requires Docker (tested with Colima on macOS).
