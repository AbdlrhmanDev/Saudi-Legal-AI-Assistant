# Source policy and data handling

## Principles

1. Only hosts in `SOURCE_ALLOWLIST` can be fetched, over HTTPS, including after redirects.
2. Every source row has a `crawl_policy`:
   - `manual` — never fetched; documents are uploaded by an admin.
   - `feed_only` — the feed is polled for detection; documents are uploaded by an admin.
   - `fetch_allowed` — the linked page may be fetched and snapshotted.
   - `fixture` — local test data only (`ALLOW_FIXTURE_SOURCES`).
3. The registry mirrors the approved source list (`legal_core/source_catalog.py`). Sources with an integrated
   connector are seeded **enabled** outside tests, because the approved list asked for them to be synchronized;
   `none` and consolidated entries are never enabled. robots.txt is honoured on every request, and a source that
   refuses automated access (403, robots.txt, firewall) is reported as blocked and never worked around. Reuse
   terms have **not** been reviewed by counsel (`licensing_status = public_terms_unreviewed`); an operator can
   disable any source from the official sources screen, and later seeding does not re-enable it.
4. Polling is infrequent (`check_interval`, default 6 h), identifies itself (`HTTP_USER_AGENT`), has timeouts
   and bounded retries.
5. A feed item is a detection signal. It never changes answers without a reviewer's approval.

## Registered sources

All 39 entries, their connectors and their measured status: [official_sources.md](official_sources.md).

### Original starting points (before the catalog)

| Source | URL | Seeded policy |
|---|---|---|
| Umm Al-Qura RSS — Decisions & Regulations | https://www.uqn.gov.sa/rssFeed/21 | `feed_only`, disabled |
| Bureau of Experts — Saudi Laws Portal | https://laws.boe.gov.sa/ | `manual`, disabled |
| HRSD — Labor Law | https://www.hrsd.gov.sa/ | `manual`, disabled |
| Ministry of Commerce — Regulations | https://mc.gov.sa/ar/Regulations/pages/default.aspx | `manual`, disabled |
| SDAIA — PDPL Knowledge Center | https://dgp.sdaia.gov.sa/wps/portal/pdp/knowledgecenter/ | `manual`, disabled |

The Umm Al-Qura site publishes several feeds: `rssFeed/21` is "قرارات وأنظمة" (decisions and laws) and is the
one registered here; `rssFeed/20` is general news. The live feed was read on 2026-10-13: 50 items, each
carrying the full text of the decision in its description. Classification of those items is a rough,
title-led triage for the reviewer, not a legal determination.

`laws.boe.gov.sa` does not send its intermediate certificate during the TLS handshake. Certificate checking
stays on; the missing intermediate is supplied from `legal_core/certs/intermediates.pem` and must itself chain
to a standard root. If the site moves to a different issuer, fetching it fails until that file is updated.

## Adding the real pilot corpus

For each document (target: 10–20 across labor, commercial, data protection):

1. Download the authoritative Arabic text from the official source, by hand if automated access is not permitted.
2. `POST /api/v1/admin/documents` with `file`, `title_ar`, `domain`, `instrument_type`, `issuing_authority`,
   `source_url` (canonical official URL), `published_at`, and `effective_from` if known.
3. Check the job result: article count, `parse_warnings`, `metadata_gaps`.
4. A legally knowledgeable reviewer confirms source, status and effective date, then approves with
   `effective_date_evidence` (the clause or gazette issue that establishes the date).
5. Record the document in a signed-off manifest with the same fields as `fixtures/corpus/manifest.json`
   (title, authority, source URL, extraction date, review status, edition, SHA-256).

Distinguish instrument types (`law`, `implementing_regulation`, `regulation`, `guidance`, `notice`, `draft`):
explanatory pages and translations are supplemental and should not be loaded as the governing text.
If two official sources disagree, do not approve either until adjudicated; the system flags overlapping
in-force versions rather than choosing one.

Do not commit bulk copies of official texts to the repository.

## Fixture data

Everything under `fixtures/` is fictional, labelled as such in its titles, uses the reserved `fixture.invalid`
domain for URLs, and is marked `is_test_data` in the database and API. One fixture document deliberately
contains hostile instructions to test source isolation.

## Personal data

- No account is needed to ask questions. Traces contain no user identifier or IP address.
- Question text is stored only when `TRACE_STORE_QUERY_TEXT=true`; otherwise only a SHA-256 hash.
- Traces are deleted after `TRACE_RETENTION_DAYS` by the worker.
- With `LLM_PROVIDER=openai` / `EMBEDDING_PROVIDER=openai`, question text and retrieved passages are sent to a
  third-party processor. Hosting region and cross-border transfer must be assessed under the Saudi PDPL and its
  implementing regulations before any real users; this project does not certify compliance. Use synthetic
  questions for the demo and tell users not to enter personal or case details.
- Secrets live in the environment only; `.env` is git-ignored and `.env.example` holds no secrets.
