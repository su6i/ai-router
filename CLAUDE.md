# CLAUDE.md — ai-router

## Project
Cost-accounting LLM gateway: one door to every model, every call tagged with
`{job, project, session}`, budgeted, and written to a ledger — so
cost-per-task is a SQL query, not a guess. Design: `docs/ARCHITECTURE.md`.

## Tech Stack
Python (uv) · Postgres + pgvector (Docker/Colima) · Prometheus + Grafana

## Relevant Skills
Read these from `.agent/constitution/skills/` before implementing domain-specific logic:
python-core-standards, python-containerization

## Key Constraints
- **uv only** — never pip directly. No new dependencies without owner approval.
- **Secrets** come from the project vault via the rule-035 resolver
  (`<vault>/secrets/.env`, layered over `_shared`); never from repo files,
  never printed.
- **Storage:** persistent data/artifacts live in the rule-035 vault
  (`<vault>/data/`), never in the repo. A local `.storage/` is scratch only
  and is git-ignored.
- **Prompt cache is exact-hash only** — semantic/RAG caching was reviewed and
  rejected; do not reintroduce it.
- **Claude models are never called from the delegate/gateway** (they are
  billed via subscription; routing them through the gateway double-bills).
- **Budget caps fail loudly** — a job over its cap aborts; silent overspend
  is forbidden.
- Docs live under `docs/`; work orders and private design docs live in the
  vault workspace and are never committed (rules 035/040/045).

## Rules & Workflows
- Rules     : `.agent/constitution/rules/` — read 000-core.md, global.md, 040-git.md before every task
- Local     : `.agent/local-rules/` — project-specific overrides (take precedence over constitution)
- Workflows : `.agent/constitution/workflows/` — pick the relevant one per task type
- Skills    : `.agent/constitution/skills/` — domain knowledge modules

## Updating the constitution
`.agent/constitution` is a symlink to one central clone shared by every project — no submodule.
```bash
git -C ~/@-github/agent-constitution pull --ff-only
```

## Global Rules
Git protocol, cost control, and code quality are in ~/.claude/CLAUDE.md (auto-loaded).
