# AI-Router — Architecture (draft for review)

> Status: **DRAFT — not yet executing.** This document is the thing we finalize
> before running anything. Open decisions are collected at the bottom.

## 1. Why this exists

Three goals, in priority order:

1. **Cost reduction** — stop burning the premium Claude (Opus) budget on grunt work.
   Route light/mechanical tasks to cheap or free models; keep the expensive brain
   for judgment, architecture, and review.
2. **Observability** — know exactly what every model costs, per run / session /
   commit / project / day, with an operational metrics stack (Prometheus + Grafana).
3. **Reuse (RAG / semantic cache)** — never pay twice for the same answer; inject
   relevant prior work as context so the premium model does less.

Secondary goal: this is a **DevOps portfolio piece** (Docker, Postgres, pgvector,
Prometheus, Grafana, migrations, 12-factor config, headless runtime).

## 2. High-level architecture

```
                         ┌─────────────────────────────────────────────┐
                         │  Opus 4.8 (orchestrator / pre-router brain)  │
                         │  classify → delegate down → review up        │
                         └───────────────┬─────────────────────────────┘
                                         │  prefix override wins; else auto-classify
              ┌──────────────────────────┼───────────────────────────────┐
              ▼                          ▼                                ▼
      ┌───────────────┐        ┌──────────────────┐            ┌──────────────────┐
      │ FREE tier     │        │ CHEAP coders     │            │ QUALITY / HEAVY  │
      │ gemini, gemma │        │ deepseek flash/  │            │ sonnet5, grok,   │
      │               │        │ pro, minimax     │            │ fable5           │
      └───────────────┘        └──────────────────┘            └──────────────────┘
                                         │
                       src/delegate.py (single gateway, provider-echoed proof)
                                         │  appends one JSON line per call
                                         ▼
                    ~/.local/share/.../ai-router/data/audit.log   (source ledger)
                                         │  ingest (idempotent, keyed by response_id)
                                         ▼
        ┌──────────────────────────────────────────────────────────────────────┐
        │  Postgres 17 + pgvector   (system of record, in Docker/Colima)         │
        │   • usage         — every call, full detail  → SQL dashboard + exporter │
        │   • prompt_cache  — prompt/response + embedding → semantic cache / RAG  │
        └──────────────────────────────────────────────────────────────────────┘
              │                         │                              │
              ▼                         ▼                              ▼
      amir router cost           prometheus-exporter            semantic cache / RAG
      (CLI, ad-hoc SQL)          (/metrics from usage)          (pre-router interception)
                                         │
                                         ▼
                               Prometheus  →  Grafana
                               (time-series)  (dashboards-as-code)
```

## 3. Components

| Component | Tech | Responsibility |
|---|---|---|
| **delegate.py** | Python + httpx | Single LLM gateway, lives at `src/delegate.py` in this repo (state — cache/audit/sessions — stays in the vault, never in git). Provider-echoed proof, cost calc, session memory, audit ledger. Claude models reachable only in the *quality/heavy* tier (see §5). |
| **Postgres + pgvector** | `pgvector/pgvector:pg17` | System of record. `usage` (ledger) + `prompt_cache` (RAG). |
| **ingest** | Python + psycopg | Idempotent load of `audit.log` → `usage` (INSERT … ON CONFLICT DO NOTHING on `response_id`). |
| **cost dashboard** | Python + psycopg | `amir router cost` — ad-hoc SQL aggregations (run/session/commit/project/model × day/week/month/year). |
| **exporter** | Python + prometheus_client | Long-running; reads `usage`, exposes `/metrics`. Solves the "CLI is too short-lived to scrape" problem — Postgres stays the single source of truth. |
| **Prometheus** | `prom/prometheus` | Scrapes the exporter; stores time-series. |
| **Grafana** | `grafana/grafana` | Dashboards provisioned as code. Two data sources: Prometheus (graphs) + Postgres (detail tables). |
| **semantic cache / RAG** | pgvector + local embeddings | Pre-router interception (see §6). |
| **local embedder** | `intfloat/multilingual-e5-small` (384-d, ONNX/CPU, offline, free) | Multilingual (fa/en/fr) prompt embeddings for the cache. Chosen for CPU/offline/free; dim 384 is wired into the schema. |

## 4. Data model

```sql
-- one row per LLM call
usage(
  response_id PK, ts timestamptz, project, commit_sha, session_id,
  model_asked, model, input_tokens, output_tokens, cache_tokens,
  cost_usd numeric(12,6), latency_s numeric)

-- semantic cache / RAG store
prompt_cache(
  id, created, project,
  repo_commit,          -- code-state key: NEVER serve an answer from a different code state
  prompt, prompt_hash,  -- prompt_hash = exact-match fast path (sha256)
  response, model,
  embedding vector(384))-- cosine HNSW index
```

`repo_commit` on the cache is the safety valve: for coding, the same prompt against
a changed codebase needs a different answer, so a hit requires matching code state.

## 5. Model fleet & task-division policy

Principle (per `cost-aware-llm-pipeline`): **start at the cheapest tier that can
plausibly do the task; escalate only on failure.** An explicit prefix from the user
always overrides the classifier.

| Tier | Models | Assigned work |
|---|---|---|
| **FREE** | gemini-2.5-flash, gemma | Trivial: classification, quick factual lookup, format/JSON conversion, first-draft prose, commit-message drafts. Rate-limited → light one-shots. |
| **CHEAP code** | deepseek-flash (default), deepseek-pro | flash: boilerplate, refactors, unit tests, docstrings, SQL, regex. pro: multi-file logic, debugging flash fails at. |
| **CHEAP reason (prepaid)** | minimax-m3 | Long-form reasoning/analysis, planning drafts, non-code writeups. **Not** clean codegen (verbose `<think>`). Spend prepaid credit first. |
| **QUALITY** | sonnet-5, grok-4.3 | sonnet-5: production code needing care, reviews of delegated output. grok: needs current knowledge or an independent second opinion. |
| **ORCHESTRATOR** | **opus-4.8 (me)** | Architecture, task decomposition, delegation decisions, reviewing/​integrating cheap-model output, final judgment. The conductor — not a grunt. |
| **HEAVY** | fable-5 | Only the hardest reasoning / long-horizon problems where Opus 4.8 is insufficient. More expensive than Opus → used rarely and deliberately. |
| **EMBEDDINGS** | e5-small (local) | Cache vectors only. Never used for chat. |

Escalation ladder for a coding task:
`gemini (if trivial) → deepseek-flash → deepseek-pro → sonnet-5 → opus-4.8 (me) → fable-5`.
Each step only if the previous output fails review.

## 6. Pre-router (mechanical delegation)

Two mechanisms so grunt work leaves Opus *without* being asked each time:

1. **Prefix hook (explicit, highest priority).** A `UserPromptSubmit` hook: if the
   prompt starts with a route tag (e.g. `r:` / `r!flash:`), the hook calls
   delegate.py, prints the cheap model's answer, and `exit 2` — Opus is skipped
   entirely (zero premium cost). *Cannot* replace the prompt, so exit-2 is the clean path.
2. **Semantic interception (RAG), auto.** On each prompt the hook computes a local
   embedding and queries `prompt_cache`:
   - **exact hash hit + same repo_commit** → print stored answer, `exit 2` (zero cost).
   - **≥0.95 cosine + same repo_commit** → (opt-in / gated) serve cached answer.
   - **0.75–0.90 cosine** → inject the related prior prompt/answer as `additionalContext`
     (RAG) so Opus answers faster/cheaper without re-searching. No blocking.

Risk note: pure semantic auto-answer for coding is gated behind `repo_commit` match
because near-identical prompts can need different answers as code changes.

## 7. Deployment

- **Runtime:** headless, no Docker Desktop GUI. **Colima** (`brew install colima`,
  `colima start`) — the `docker` CLI + `docker compose` bind to it unchanged. Podman
  is the rootless alternative. Avoids Docker Desktop's org licensing too.
- **Compose profiles:** `db` starts by default; `--profile monitoring` adds
  prometheus + grafana + exporter (Phase 2).
- **12-factor:** all config via `.env` (git-ignored); `DATABASE_URL` is the contract.
- **Persistence:** named volume `pgdata`; healthcheck via `pg_isready`.
- **Schema:** `db/init/*.sql` runs on first cluster init; later changes go through
  numbered migrations.

## 8. Phased roadmap

- **Phase 1 — data plane:** Postgres+pgvector (Docker/Colima) + schema + ingest +
  `amir router cost` (SQL). Everything else depends on this.
- **Phase 2 — observability:** exporter + Prometheus + Grafana (dashboards-as-code).
- **Phase 3 — RAG / semantic cache:** local embedder + prompt_cache + pre-router hook.

## 9. Open decisions (to finalize before executing)

1. **Do Sonnet 5 / Fable 5 enter delegate.py** (via pay-per-token API keys), or do
   they stay as "me (Opus) escalating manually"? Adding them to the router spends
   real money per call — is that wanted, or keep the router cheap/free-only and let
   the Claude tier be me?
2. **Runtime:** Colima (recommended, headless) vs keep the already-running Docker
   Desktop for now?
3. **Embedding model / dim:** e5-small (384, lighter) vs bge-m3 (1024, stronger
   multilingual, heavier). Changing later means a schema migration.
4. **Repo home & name:** `~/@-github/ai-router` (current) — good, or rename?
5. **Semantic auto-answer at ≥0.95:** enable, or keep it opt-in / context-injection
   only (safer for live coding)?
