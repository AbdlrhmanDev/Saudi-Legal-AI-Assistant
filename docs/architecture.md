# Architecture

```
Browser (Arabic chat / admin)                [apps/web — built separately]
            │  HTTPS, JSON
      FastAPI REST API  (services/api)
        │            │
   Chat RAG      Operator API (runs, quarantine, ──► ingestion_jobs (queue in PostgreSQL)
        │         generations, rollback)                    │
 Hybrid retriever    │                     Worker (services/worker) × 1
        │            │                     00:00 Asia/Riyadh sync · catch-up · jobs · heartbeat
        └────────────┴──────────┬───────────────────┘
                     PostgreSQL + pgvector
        instruments → versions → articles → chunks (tsvector + vector)
        sync_runs · source_snapshots · index_generations · quarantine_events
        amendment_relations · update_candidates · review_events · query_traces
                                │
                 enabled, allowlisted official sources
```

The nightly pipeline, release gate, scheduling contract and PRD traceability are documented in
[autonomous_sync.md](autonomous_sync.md); capacity and deployment topology in [scaling.md](scaling.md).

All domain logic lives in `packages/legal_core`; the API and the worker are thin shells over it.

## Data model and invariants

`legal_instruments 1─* document_versions 1─* articles 1─* article_chunks`

- A version is an immutable snapshot (raw hash, extracted text, parsed articles). Article text is never updated.
- Two separate time axes: `retrieved_at` (when the system observed it) and `effective_from` /
  `effective_to` (legal validity, `effective_to` exclusive). `published_at` is stored but never used as validity.
- Retrievable ⇔ `review_status = 'approved' AND index_active AND effective_from ≤ as_of AND (effective_to IS NULL OR effective_to > as_of)`.
- `CHECK (review_status <> 'approved' OR effective_from IS NOT NULL)` — the database itself refuses
  authoritative content without a confirmed effective date.
- `UNIQUE (instrument_id, raw_hash)` — idempotent ingestion. `UNIQUE (source_id, external_id, changed_hash)` — idempotent feed reads.
- `review_events` is append-only; rollback deactivates, it does not delete.

Official-source and judicial tables (migration 0006):

| Table | Purpose |
|---|---|
| `source_registry` | All 39 approved sources: category, authority tier, document kind, connector, health, integration status, access notes |
| `source_sync_logs` | One row per connector run per source: status, HTTP status, counts, failure reason |
| `judicial_decisions` | Judgments and principle collections (court, level, Hijri/Gregorian date, finality, text, snapshot); never legislation |
| `document_relationships` | Instrument-to-instrument links (implementing regulation, amends, repeals) |

Views with the names used in the integration specification read the same rows: `official_sources`,
`source_connectors`, `legal_documents` (= `legal_instruments`), `legal_document_versions`, `legal_articles`,
`legal_amendments` (= `amendment_relations`), `legal_chunks` (= `article_chunks`), `retrieval_audit_logs`
(= `query_traces`). `ingestion_jobs` is a table. A later migration that alters one of the aliased tables must drop
and recreate its view.

Question analysis (`legal_core/intent.py`) runs before retrieval: the domain guard returns the fixed
out-of-scope sentence for non-legal questions, and recognised legal intents add a formal-Arabic search query.
See [dialect_and_scope.md](dialect_and_scope.md).

## Status model

Stored (version): `needs_review → approved | quarantined | rejected`, `approved → rolled_back`, and
`evidence` for an official notice (e.g. a repeal decision) kept as proof but never retrievable.
Derived from the as-of date (never stored): `scheduled → active → superseded | repealed`
(`legal_core/temporal.py`). This is why “approved” is not the same as “in force”.

Candidates: `candidate → retrieved → needs_review →` (release gate) `→ approved | quarantined | excluded`.
`failed` (fetch error) and transient quarantine reasons are retried by later runs; an operator can still
approve or reject a quarantined candidate (escape hatch, PRD v2 8.2).

## Ingestion

`sniff (magic bytes) → extract (PyMuPDF / HTML with active content removed / UTF-8) → parse → store version (needs_review) → candidate + diff`

The parser recognises `المادة الأولى`, `المادة الحادية والعشرون بعد المائة`, `المادة (5)`, `المادة ٥:`, `مكرر`,
and `الباب/الفصل` headings. A line is a heading only if the ordinal parses and is followed by end-of-line or a
separator, so in-body references do not split articles. Structural anomalies (gaps, duplicates, no articles)
become `parse_warnings` for the reviewer.

## Retrieval (PRD 9.1)

1. Normalize the query; extract article hints (“المادة الخامسة”) and statute-name hints.
2. Filter in SQL: review state, index activation, effective interval, domain.
3. Lexical ranking: PostgreSQL full-text (`simple` config) over light-stemmed Arabic tokens, pool re-ordered by
   number of distinct query stems covered (ts_rank has no IDF). Trigram index serves catalog title search.
4. Vector ranking: pgvector cosine (HNSW, iterative scan so filters do not starve results).
5. Reciprocal rank fusion (k = 60) over lexical, vector and hint rankings; chunks map back to whole articles.
6. Evidence gate: lexical coverage ≥ `MIN_LEXICAL_COVERAGE`, or semantic similarity ≥ `MIN_VECTOR_SIMILARITY`
   (real embeddings only), or an explicitly requested article of a named statute. Instruction-like passages are quarantined.

RRF was chosen because the two score scales are not comparable and it needs no tuning data; the thresholds
are configuration and should be re-fitted on the real corpus.

## Answering (PRD 9.2–9.3)

No strong evidence → fixed abstention text, model not called. Otherwise the model receives the question and
evidence as one JSON document and must return `{answer_ar, answer_status, citations: [evidence ids], caveats}`
(strict JSON schema). The server validates the draft (ids exist, at least one citation, article numbers in the
prose come from cited passages), retries once with the validator's reason, then falls back to a verbatim quote
marked `partial`. Citation objects are built from database rows, never from model output. Temporal caveats and
the `forthcoming` list are added server-side.

## Update lifecycle (PRD v2 section 8)

| Step | Where |
|---|---|
| Discover / de-duplicate | `monitor.sync_source` — allowlist check, retries with backoff, canonical URL + item id + content hash |
| Fetch & snapshot | `monitor.acquire` (only when `crawl_policy` allows it) → immutable `source_snapshots` row |
| Parse & align | `ingestion.ingest_document` + `diffing.diff_against_base` |
| Classify & validate | `agent` proposal → deterministic `release.evaluate`; quarantine with a reason code otherwise |
| Version, index, activate | `sync._publish` — interval changes, incremental embeddings and a health-checked generation switch in one transaction |
| Observe | `sync_runs.stage_counts`, `review_events` (actor = null for automatic decisions), alerts, freshness report |

Manual review (`review.approve_candidate`) remains as an escape hatch and goes through the same
`place_version` + `activate_generation` path.

## Operations

- Jobs: `SELECT … FOR UPDATE SKIP LOCKED`; each job in its own transaction; permanent errors fail, transient ones retry.
- `/readyz` = database + pgvector (worker reported, not gating); `/healthz` = process up.
- `/health/ready` (legacy) additionally requires a worker heartbeat younger than `WORKER_STALE_SECONDS`.
- Structured JSON logs with request id; paths only (no query strings, bodies, question text or tokens).
- Traces store a hash of the question, retrieved/cited ids, scores, timings and models; purged after `TRACE_RETENTION_DAYS`.
