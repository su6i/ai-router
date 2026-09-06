# Code-Aware RAG (Phase 3b)

Semantic retrieval over **code** (function/class chunks), the way `r rules`
(Phase 3a) retrieves rule/doc text. Same Postgres + pgvector store, same local
embedder, plus a static call graph. Exposed as `r code` (CLI) and
`code_lookup` (MCP).

## Architecture

```mermaid
graph TD
    A[Tracked source files<br/>git ls-files *.py *.sh *.js *.ts *.tsx *.jsx *.go *.rs] -->|"chunk-files child process<br/>(tree-sitter for .py/.sh,<br/>generic whole-file for the rest)"| B(AST chunks: function/class/method<br/>+ generic ~400-tok blocks for other langs)
    B -->|JSON over stdout| C[Parent process]
    C -->|"synthetic header + passage:"| D[E5 ONNX embedder<br/>intfloat/multilingual-e5-small]
    C -->|stdlib ast| G[code_edges<br/>caller → callee]
    D --> E[(Postgres<br/>code_chunks + pgvector HNSW)]
    G --> E
    F[Query] -->|"query:"| D2[E5 embedder]
    D2 -->|cosine top-k| E
    E -->|"--graph: +1-hop callers/callees"| H[chunks with path:start-end refs<br/>capped ~2k tokens]
```

## Design decisions

- **tree-sitter for chunk boundaries, stdlib `ast` for call edges.**
  tree-sitter gives byte-exact function/class/method boundaries for both
  Python and Bash through one API (the language registry is a table — adding
  a grammar later is one entry). Call-edge extraction only needs Python and
  is much simpler over `ast.NodeVisitor` than over tree-sitter queries.
- **pgvector over LanceDB/Faiss** — the Postgres+pgvector store is already
  running for Phase 3a; one store, one backup path, one HNSW index pattern
  (`rules_chunks` and `code_chunks` are siblings).
- **e5-small ONNX on CPU** — same embedder as Phase 3a: local, free,
  multilingual (fa/en/fr queries hit English code identifiers), 384-d.
- **Synthetic header before embedding** — each chunk is embedded as
  `"<lang> <symbol> in <path>"` + body, which lifts recall on symbol-name
  queries for near-zero cost.
- **Static-only call graph** — `code_edges(caller_id, callee_symbol,
  resolved_id)` from intra-repo `ast` analysis; `--graph` expands top-k hits
  with 1-hop callers/callees. No dynamic analysis, no cross-repo edges:
  retrieval expansion doesn't need them and they'd cost far more than they
  return.
- **Oversized definitions** (> ~400 est. tokens) are split at inner block
  boundaries; every sub-chunk is re-prefixed with the enclosing signature
  line so it stays self-describing. Token counts use a chars/3 estimate —
  chunk-size control doesn't need tokenizer precision.
- **Process isolation + version pin for tree-sitter (empirical).**
  py-tree-sitter **0.26.0** deterministically segfaulted on macOS arm64 in
  this workload — three independent repros: (1) walking real-size files with
  a live HF `tokenizers` object in the process, (2) interleaving parses with
  onnxruntime embedding calls, (3) reusing one `Parser` across files. The
  dependency is therefore pinned to `>=0.25,<0.26` (0.25.2 passes all
  repros 3/3), **and** chunking runs in a dedicated `chunk-files` child
  process that never creates tokenizer/ONNX objects while the parent
  (embedding + DB) never parses — defense in depth against a known-flaky
  native binding.
- **Iterative AST walk** — the chunker walks materialized `node.children`
  lists with an explicit stack; recursive cursor traversal was part of the
  0.26.0 crash surface and is avoided.
- **Clean module boundary** — `src/code_index.py` + its two tables are
  reachable only through the CLI/MCP query API; nothing inside
  `delegate.py` imports it. The whole feature is extractable as a
  standalone tool.

## Model lifetime

The `intfloat/multilingual-e5-small` model is loaded via ONNX Runtime as a process-wide singleton (`get_model()`). An idle-unload thread automatically frees the model after a period of inactivity to return memory to the OS, governed by the `RAG_MODEL_IDLE_TTL` environment variable (defaults to 900 seconds; set to 0 to disable unloading).

## Incremental reindex

`r code --reindex` diffs `indexed_commit..HEAD` (`git diff --name-only`),
re-chunks only changed files, upserts by `chunk_hash` (unchanged chunks are
re-stamped, not re-embedded), deletes chunks of vanished files/symbols, then
stamps the new `repo_commit`. `--rebuild` re-discovers everything via
`git ls-files -- '*.py' '*.sh' '*.js' '*.jsx' '*.ts' '*.tsx' '*.go' '*.rs'`
(`CODE_GLOBS` — tracked files only, vendored/venv paths can never enter the
index). Both paths are idempotent; a second run is a no-op. Queries print a
one-line stale-index warning when `repo_commit != HEAD`.

## When it pays off

Honest economics (carried over from the wo-0012 appendix): code retrieval
starts paying for itself on repos **>50 kLOC**, or when **>30% of worker
turns are exploratory reads** despite the repo map. ai-router itself is
~6 kLOC — at this size the repo map alone already answers most "where is X"
questions and the win below is real but modest. The build is justified as
groundwork + portfolio, not by this repo's size.

## Measurement (2026-07-18, live index of this repo)

Task: *"explain how budget caps abort a delegation"*. Three briefings,
character counts measured, token counts = chars/4 (ESTIMATE):

| Briefing | Chars | Est. tokens | vs (a) |
| --- | --- | --- | --- |
| (a) whole relevant files (`delegate.py` + `test_budgets.py`) | 70,837 | ~17,709 | — |
| (b) repo map only | 3,947 | ~986 | −94.4% |
| (c) repo map + `r code` top-5 | 6,526 | ~1,631 | −90.8% |

(b) is cheapest but only names symbols; (c) additionally carries the actual
`check_budget._check` implementation the task asks about, at ~9% of the
whole-file cost. Extrapolation: on a 50 kLOC repo the "whole relevant files"
baseline grows roughly linearly with module size while (c) stays capped at
~2k tokens by construction.

Retrieval quality spot-checks (live):
`r code "where is the budget cap checked"` → top-1
`src/delegate.py:338-346 [check_budget._check]`; `--graph` additionally
pulls the true callers `check_budget` and `test_budget_abort`.

## Multi-repo ingestion (T-953)

Before this, `code_index.py` hardcoded `root = Path(__file__).resolve().parent.parent`
— it only ever indexed ai-router's own checkout, so an agent working in any
other repo had no code memory at all. Every repo under `$HOME/@-github/` is
now indexed into the same `code_chunks` table, discriminated by `repo`.

**Repo roots come from config, not from a path constant.** `get_repo_roots()`
reads `<vault>/data/code_repo_roots.json` (`{"roots": ["/abs/path", ...]}`, a
bare JSON list is also accepted). Adding or removing a repo from the sweep is
a config edit, never a commit. A missing, unreadable, or invalid config falls
back to `_default_repo_roots()` — every directory directly under
`$HOME/@-github/` that contains a `.git` — which is the default with no
config file present at all.

**Exclusions.** `_is_excluded()` skips `node_modules/`, `.venv/`, `venv/`,
`dist/`, `build/`, `__pycache__/`, `.git/`, and `*.min.js` even if a repo
happens to have committed one of those paths — defense in depth on top of
`git ls-files` only ever seeing tracked files in the first place.

**Multi-language chunking.** `CODE_EXT_LANG` maps every extension this
indexer treats as code: `.py .sh .js .jsx .ts .tsx .go .rs`. Only `.py` and
`.sh` get real AST-based chunking through the tree-sitter grammars vendored
in this repo (`tree_sitter_python`, `tree_sitter_bash`) — function/class
boundaries, symbol names, the oversized-def splitter. Grammars for the rest
(`tree-sitter-javascript`/`-typescript`/`-go`/`-rust`) are new dependencies
that need explicit owner approval and were out of scope for this fix, so
those extensions go through `chunk_generic()` instead: the whole file is
split into ~400-token line-blocks with no symbol/parent extraction
(`symbol: null`). Coarser than the AST path, but it makes the file
searchable at all — before this fix a 100%-JS/TS repo (`portfolio`,
`parsi-rtl`, `parsi-rtl-test`) contributed exactly 0 chunks (T-953 defects
#1/#2), which is worse than coarse chunking.

**The only file that legitimately yields 0 chunks is one with 0 bytes of
real (non-whitespace) content.** The AST chunker only emits a chunk for a
`function_definition`/`class_definition` node, so a pure top-level `.py`/
`.sh` script (imports + calls, no functions) used to walk to nothing —
this is what was silently swallowing all 29 of `polycast`'s
`experiments/gemini/scripts/*.py` and 4 `.sh` scripts elsewhere (T-953
follow-up audit). `cmd_chunk_files()` now falls back to `chunk_generic()`
(the same whole-file/~400-token-block chunker used for js/ts/go/rust)
whenever the AST walk for a `.py`/`.sh` file finds zero `def`/`class`
nodes, so real top-level code is searchable at whole-file granularity
instead of being invisible. A file whose content is entirely empty or pure
whitespace still — correctly — produces 0 chunks: confirmed against
several 0-byte `__init__.py` files in `Arix` and `cisco-manager`, there is
nothing to embed. The file is always hashed and recorded in
`ingested_files` regardless of chunk count, so a 0-chunk file is not
silently skipped — it is chunked with a genuinely empty result and never
re-chunked until its content changes.

**Per-repo isolation and failure isolation.** `ingest(force, repo_path=...)`
now takes an explicit repo root instead of always using `Path.cwd()`; every
DB read/write it does is scoped to that repo's name, which for the sweep
path is **the root directory's own basename** — never the git remote URL.
Two different checkouts can share (or be misconfigured to share) one git
remote (T-953 defect: `parsi-rtl-test`'s `origin` was still `parsi-rtl.git`),
and remote-derived identity let them collide under one `code_chunks.repo`
value, with a `--force` sweep of one silently GC-deleting the other's rows.
`get_repo_roots()` already guarantees each swept root is a distinct sibling
directory, so its basename is both unique and matches the casing everyone
actually uses (previously "arix" from the lowercase remote vs. the real
`Arix` directory on disk). The single-repo `project_info()` used by `r code`
when run from inside a checkout is unchanged (still remote-derived, matching
the rest of the ledger's project tagging) — only the multi-repo sweep's
`_project_info_for()` changed. `sweep()` calls
`ingest()` once per configured root and catches any exception per repo
(bad encoding, no git, broken symlink, permission-denied directory, ...),
logging and moving on to the next repo — one bad repo never aborts the
sweep. The one exception that is deliberately NOT swallowed is
`psycopg.OperationalError`: Postgres being down is not a per-repo problem,
so it propagates so the caller reports the real outage instead of it
looking like 30-odd unrelated repo failures. A single unreadable file inside
an otherwise-good repo is isolated at an even finer grain — `ingest()`
skips just that file (logged, `files_failed` counted) and keeps indexing the
rest of the repo.

**Sweep budget and resume.** `sweep(budget_seconds=...)` (env
`RAG_CODE_SWEEP_BUDGET_S`, default 1500s) stops starting new repos once the
budget is spent — a repo already in progress finishes, but the next one is
deferred. Which repo to resume from is persisted round-robin in
`<vault>/data/code_sweep_state.json` (`{"next_index": N}`), so a cold repo
that eats the whole 30-minute launchd window on one run does not starve the
same tail of the repo list on every subsequent run — the next sweep picks up
where the last one left off. `src/rag_ingest.py --collection code` calls
`sweep()` (not the single-repo `ingest()`) precisely so the launchd job
`com.ai-router.rag-sweep` gets this behavior; `r code --reindex`/`--rebuild`
(and `--receipt`'s single-file ingest) still call `ingest()` directly,
scoped to the cwd's own repo, unchanged.

**Cross-repo search.** `r code --all-repos "<query>"` (and the `code_lookup`
MCP tool's `all_repos: true` argument) searches every repo's chunks instead
of just the cwd-inferred one, and prefixes each hit with its repo name
(`[repo] path:start-end [symbol]`) — this labelling is what makes a snippet
written in one repo findable and reusable from another. Default behavior
(no `--all-repos`) is unchanged: one repo, no prefix, same output format as
before this change.
