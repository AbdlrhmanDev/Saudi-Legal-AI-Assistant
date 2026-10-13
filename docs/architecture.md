# Architecture

```
Browser (Arabic chat / admin)                [apps/web — built separately]
            │  HTTPS, JSON
      FastAPI REST API  (services/api)
        │            │
   Chat RAG      Admin + update review ──► ingestion_jobs (queue in PostgreSQL)
        │            │                              │
 Hybrid retriever    │                     Worker (services/worker)
        │            │                     jobs · APScheduler · heartbeat
        └────────────┴──────────┬───────────────────┘
                     PostgreSQL + pgvector
        instruments → versions → articles → chunks (tsvector + vector)
        source_registry · update_candidates · review_events · query_traces
                                │
                 approved, allowlisted official sources
```

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

## Status model

Stored (ingestion review): `needs_review → approved | rejected`, `approved → rolled_back`.
Derived from the as-of date (never stored): `scheduled → active → superseded | repealed`
(`legal_core/temporal.py`). This is why “approved” is not the same as “in force”.

Candidates: `candidate → retrieved → needs_review → approved | rejected`, with `failed` as a retryable side path
(attach the document by manual upload).

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

## Update lifecycle (PRD 8.1)

| Step | Where |
|---|---|
| Detect / de-duplicate | `monitor.sync_source` — allowlist check, retries with backoff, canonical URL + item id + content hash |
| Acquire | only when the source's `crawl_policy` allows it (`fetch_allowed`, `fixture`); otherwise manual upload |
| Extract & align | `ingestion.ingest_document` + `diffing.diff_against_base` |
| Verify | `review.approve_candidate` / `reject_candidate` — evidence text mandatory, warnings acknowledged |
| Version & index | approval closes the predecessor's interval and builds chunks/embeddings in the same transaction |
| Observe | job history, source `last_error`, traces, audit trail; `tests/evaluation` as the regression run |

## Operations

- Jobs: `SELECT … FOR UPDATE SKIP LOCKED`; each job in its own transaction; permanent errors fail, transient ones retry.
- `/health/ready` = database + pgvector extension + a worker heartbeat younger than `WORKER_STALE_SECONDS`.
- Structured JSON logs with request id; paths only (no query strings, bodies, question text or tokens).
- Traces store a hash of the question, retrieved/cited ids, scores, timings and models; purged after `TRACE_RETENTION_DAYS`.
