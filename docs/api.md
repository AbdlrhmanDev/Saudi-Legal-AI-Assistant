# API guide for the frontend

Base URL `http://localhost:8000`. Interactive docs: `/docs`. Machine-readable spec: `/openapi.json`.

## Conventions

- JSON everywhere (document upload is `multipart/form-data`).
- Success: `{"success": true, "data": …, "message"?: "…"}`
- Error: `{"success": false, "error": {"code": "…", "message": "…", "details": {…}}}`
- Status codes: 200, 201, 401, 403, 404, 409 (invalid state), 422 (validation), 429 (rate limit), 500.
  `/health/ready` returns 503 when not ready.
- Lists: `?page=1&limit=20` → `data.items` + `data.pagination {page, limit, total, totalPages}`.
- Auth: `Authorization: Bearer <access_token>`. Admin routes require role `admin` (checked server-side).
- Every response carries `X-Request-ID`. CORS origins come from `CORS_ORIGINS`.
- Unknown JSON fields are rejected (422).
- **Render all text fields as plain text, never as HTML.** Article text and feed titles are untrusted.

## Auth

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/v1/auth/login` | `{email, password}` | → `{access_token, token_type, expires_in, user}` |
| POST | `/api/v1/auth/register` | `{name, email, password, accept_terms: true}` | → 201, same shape as login. Role is always `user`. Password: 10+ chars, a Latin letter and a digit. 409 `EMAIL_TAKEN` |
| POST | `/api/v1/auth/forgot` | `{email}` | always 200 `{sent: true}`; outside production also `dev_reset_token` (no mail service is configured) |
| POST | `/api/v1/auth/reset` | `{token, password}` | single-use, 30-minute token; ends earlier sessions. 422 `INVALID_RESET_TOKEN` |
| GET | `/api/v1/auth/me` | | current user |

## Chat (public)

`POST /api/v1/chat`

```json
{ "query": "ما الذي ينص عليه نظام العمل بشأن فترة التجربة؟", "domain": "labor", "as_of_date": "2026-10-12" }
```

`domain` ∈ `labor | commercial | data_protection | other` (optional). `as_of_date` optional (default: today, Riyadh).

```json
{
  "success": true,
  "data": {
    "answer_ar": "…",
    "answer_status": "grounded | partial | insufficient_evidence",
    "as_of_date": "2026-10-12",
    "citations": [
      {
        "law_id": "uuid", "article_id": "uuid", "version_id": "uuid", "source_url": "https://…",
        "law_title_ar": "…", "article_number": "5", "article_label": "المادة الخامسة", "chapter": "…",
        "quote": "full article text", "effective_from": "2024-01-01", "effective_to": null,
        "legal_status": "active", "is_test_data": true
      }
    ],
    "caveats": ["…"],
    "trace_id": "uuid",
    "disclaimer_ar": "…",
    "forthcoming": [
      { "law_id": "uuid", "law_title_ar": "…", "version_id": "uuid", "effective_from": "2026-11-01", "legal_status": "scheduled" }
    ]
  }
}
```

UI mapping (PRD 9.4 / 12.3): `answer_ar` → direct response; `citations` → legal basis with article label,
“open source” action (`source_url`) and supporting passage (`quote`); `as_of_date` + citation
`effective_from/effective_to` + `forthcoming` → state/date banner; `caveats` and `disclaimer_ar` → notices.
For `insufficient_evidence`, `citations` is always empty — show the limitation message, no legal basis section.
Show an `is_test_data` badge wherever it is true.

## Catalog (public, reviewed material only)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/laws?q=&domain=&instrument_type=&page=&limit=` | title search is typo-tolerant |
| GET | `/api/v1/laws/{id}` | |
| GET | `/api/v1/laws/{id}/versions?as_of_date=` | each version has `legal_status` relative to `as_of_date`: `scheduled | active | superseded | repealed` |
| GET | `/api/v1/versions/{id}/articles` | full article list of one version |
| GET | `/api/v1/articles/{id}` | article + version + law (for “inspect passage”) |
| GET | `/api/v1/coverage` | instruments/articles per domain + coverage notice |

## Admin: corpus

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/admin/documents` | multipart. Exactly one of `file` (PDF/HTML/UTF-8 text, ≤ 20 MB) or `register_url` (https, allowlisted host). Optional fields: `title_ar, domain, instrument_type, issuing_authority, source_url, version_label, issued_at, published_at, effective_from, instrument_id, candidate_id, is_test_data`. → 201 `{job_id, status}` |
| GET | `/api/v1/admin/documents?review_status=&instrument_id=&domain=` | every version incl. unreviewed; has `review_status, index_active, metadata_gaps, parse_warnings` |
| GET | `/api/v1/admin/documents/{version_id}` | version + articles + review events |
| POST | `/api/v1/admin/versions/{id}/rollback` | `{reason}` |
| POST | `/api/v1/admin/versions/{id}/reindex` | → 201 `{job_id}` |

Use `candidate_id` on upload to attach the official text to a feed candidate that could not be fetched automatically.
`metadata_gaps` values: `source_url, source_url_not_allowlisted, issuing_authority, published_at, effective_from`.
`parse_warnings` examples: `no_articles_detected, duplicate_article_number:N, article_number_gap:A->B, possible_prompt_injection`.

## Admin: jobs

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/admin/jobs?status=&job_type=` | `job_type`: `document_ingest | feed_sync | reindex`; `status`: `queued | running | succeeded | failed | duplicate` |
| GET | `/api/v1/admin/jobs/{id}` | poll after upload/sync. `result` (ingest): `{version_id, instrument_id, candidate_id, duplicate, article_count, warnings, metadata_gaps}`; `counts` (sync): `{sources_checked, sources_failed, seen, new, changed, unchanged, skipped, acquired, acquire_failed}`; `errors[]`, `retry_count` |

## Admin: update review

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/admin/updates?state=&classification=` | queue. `state`: `candidate | retrieved | parsed | needs_review | approved | rejected | failed` |
| POST | `/api/v1/admin/updates/sync` | optional `{source_id}` → 201 `{job_id}` |
| GET | `/api/v1/admin/updates/{id}` | `candidate` (with `summary`, `diff`), `proposed_version`, `review_events` |
| PATCH | `/api/v1/admin/updates/{id}` | `{classification?, target_instrument_id?, reason?}` |
| POST | `/api/v1/admin/updates/{id}/approve` | `{effective_from?, effective_date_evidence, published_at?, reason?, acknowledge_warnings?}` |
| POST | `/api/v1/admin/updates/{id}/reject` | `{reason}` |

Only candidates in `needs_review` (official text attached) can be approved. A `candidate`-state item needs an upload first.

`diff` shape:

```json
{
  "base_version_id": "uuid | null",
  "summary": { "added": 0, "changed": 1, "removed": 0, "unchanged": 9 },
  "affected_article_numbers": ["5"],
  "changes": [
    { "article_number": "5", "change": "changed | added | removed",
      "before": "…", "after": "…", "snippet": ["@@ …", "-old line", "+new line"],
      "before_article_id": "uuid", "after_article_id": "uuid" }
  ],
  "warnings": ["possible_partial_amendment_document", "no_base_version"]
}
```

Approve error codes to handle: `EFFECTIVE_DATE_REQUIRED` (422), `WARNINGS_NOT_ACKNOWLEDGED` (409 — show the
warnings, resubmit with `acknowledge_warnings: true`), `CONFLICTING_VERSION` (409), `NO_DOCUMENT` (409), `INVALID_STATE` (409).

## Admin: operations

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/admin/stats` | corpus counts, queue by state, jobs by status, recent failed jobs, sources, last successful check, chat latency p50/p95, worker liveness |
| GET | `/api/v1/admin/sources` · PATCH `/api/v1/admin/sources/{id}` | `{enabled?, check_interval?}` (minutes). Enabling a `manual` source → 409 `SOURCE_MANUAL_ONLY` |
| GET | `/api/v1/admin/traces?response_status=` · GET `/api/v1/admin/traces/{id}` | detail has `retrieved[]` with `rank, rrf_score, lexical_rank, lexical_score, vector_rank, vector_similarity, lexical_coverage, strong, quarantined` |
| GET | `/api/v1/admin/audit?decision=` | review events: `approved | rejected | rolled_back | classification_override` |
| GET | `/api/v1/admin/evaluation` | latest measured evaluation report (404 until generated) |
| GET | `/health/live` · `/health/ready` | plain JSON, no envelope |
