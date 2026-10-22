# Autonomous nightly synchronization (PRD v2)

Every day at **00:00 Asia/Riyadh** the worker runs one logical synchronization. Changes that pass
every deterministic check are published with no human action; anything uncertain is quarantined with
a reason code and the last trusted corpus keeps serving answers.

## Pipeline

| Stage | Code | Result |
|---|---|---|
| 1 Discover | `monitor.sync_sources` → `connectors.discover` per source (RSS, document listings, MOJ judgments API, page change detection) + `sync._supplementary_discovery` (bounded agent search, allowlist only) | Candidate per new/changed document; judgments into `judicial_decisions`; a `source_sync_logs` row and health status per source |
| 2 Fetch | `monitor.acquire` → `snapshots.store_snapshot` | Immutable bytes + SHA-256 in `source_snapshots`; SSRF/redirect/MIME/size/robots checks |
| 3 Deduplicate | feed identity + content hash; one candidate per canonical URL across sources (a Bureau of Experts page linked from a ministry is attributed to the Bureau); `(source, checksum)` snapshots; `(instrument, raw_hash)` versions | Unchanged items and documents are skipped; documents beyond the per-source politeness cap wait as backlog; known laws are re-read every `CONNECTOR_REFRESH_DAYS` to detect amendments |
| 4 Parse | `ingestion.ingest_document` | Article-level text, normalized search text, parse warnings |
| 5 Classify | `agent.RuleClassifier` or `agent.OpenAIClassifier` (structured output, verified against the source) | Proposal: type, target instrument, quoted date spans |
| 6 Validate | `release.evaluate` (deterministic gate) + `legal_dates.resolve_effective_date` | publish / quarantine (reason code) / exclude |
| 7 Version | `review.place_version` (amendments), repeal handling in `sync._publish_one` | New interval; predecessor ends; nothing overwritten |
| 8 Re-index | `indexing.index_version` | Embeddings only for changed text (content-hash reuse) |
| 9 Activate | `generations.activate_generation` | Health checks, then a new active `index_generations` row in the same transaction |
| 10 Observe | `review_events` (auto_published / auto_quarantined / auto_excluded), `sync_runs.stage_counts`, `alerts.emit` | Audit trail, dashboard, optional webhook |

## Release gate (no confidence-score publishing)

A candidate is published only if all of these hold, in this order; the first failure is its reason code.

| Check | Reason code when it fails |
|---|---|
| Agent and rules agree (model may only reclassify inconclusive decree/decision/circular items) | `AGENT_RULES_DISAGREE` |
| Source publishes legislation (Istitlaa drafts, news, directories and archives are excluded whatever the item title says) | `NOT_A_LEGAL_CHANGE` (excluded) |
| Type is new law / amendment / repeal / regulation (drafts, notices, news are excluded) | `NOT_A_LEGAL_CHANGE` (excluded) |
| Enabled, allowlisted official source over HTTPS | `NOT_OFFICIAL_SOURCE` |
| Official text retrieved and snapshotted | `NO_OFFICIAL_TEXT`, `SOURCE_UNAVAILABLE` (retried) |
| No instructions addressed to an AI system in the document | `PROMPT_INJECTION_DETECTED` |
| Valid article structure, no duplicate article numbers | `ARTICLE_STRUCTURE_INVALID`, `CONFLICTING_ARTICLE_NUMBERS` |
| Amended / repealed instrument resolved and authoritative | `TARGET_UNRESOLVED` |
| Consolidated text, not a partial amendment | `AMBIGUOUS_AMENDMENT_MAPPING` |
| Text actually differs from the version in force | `NO_TEXTUAL_CHANGE` (excluded) |
| Exactly one evidenced effective date — or, for a Bureau of Experts consolidated law whose text states none, the Bureau's official status «ساري», which verifies it in force from the day its page was retrieved (never back-dated) | `EFFECTIVE_DATE_MISSING`, `CONFLICTING_EFFECTIVE_DATES`, `HIJRI_DATE_UNVERIFIED`, `PUBLICATION_DATE_MISSING`, `STATUS_NOT_IN_FORCE` |
| No other authoritative version starts that day / repeal follows the versions it ends | `CONFLICTING_EFFECTIVE_VERSION` |
| Staged index passes health checks | `INDEX_HEALTH_FAILED` (retried) |

Effective dates are read only from commencement clauses ("يعمل به اعتباراً من …", "يسري من تاريخ
نشره", "بعد تسعين يوماً من تاريخ نشره"). Publication date is used only when the text itself says the rule
applies from publication. In a consolidated amended text, the statute's own "from its publication"
clause is ignored (it refers to the original law); the amendment's date must come from the notice or
an explicit date.

## Scheduling contract

- Run key `daily:<Riyadh date>` is unique: one logical run per Saudi calendar day.
- Advisory lock `pg_try_advisory_lock`: one active run across all processes.
- In-process schedule: APScheduler `CronTrigger(hour=0, minute=0, timezone="Asia/Riyadh")` plus a
  5-minute tick that catches up missed days (`SYNC_CATCHUP_DAYS`) and retries failed runs with
  exponential backoff (`SYNC_MAX_RETRIES`, `SYNC_RETRY_BACKOFF_SECONDS`).
- Alternative: Kubernetes CronJob (`deploy/k8s/nightly-sync-cronjob.yaml`) calling
  `python -m legal_core.cli nightly-sync`.
- A run left `running` by a dead process is marked `WORKER_LOST` and retried.
- Statuses: `succeeded`, `no_op` (nothing new; corpus unchanged), `partial` (some sources failed),
  `failed` (all sources failed or an error; retried).
- When a run finds a new/changed item, the Operations dashboard records discovered, published and
  quarantined counts. If `ALERT_WEBHOOK_URL` is configured, the same result is sent to the operator;
  successful runs with no changes do not send a notification.

## Index generations and rollback

Each activation records a generation with a manifest of the served versions and their intervals.
`POST /api/v1/admin/index/rollback` re-applies an earlier manifest (chunks are kept, so no
re-embedding) and records the rollback as a new generation, so it can itself be undone. Health
checks: document count, affected articles indexed, embedding completeness, citation resolution,
sample lexical recall, non-overlapping intervals.

## Freshness shown to users

`GET /api/v1/freshness` and every chat answer carry `corpus_verified_at`, `source_health`
(`healthy | delayed | degraded`), covered domains, the latest included official publication and the
next scheduled sync. When health is not `healthy`, answers include a warning instead of implying the
corpus is current.

## PRD traceability

| PRD item | Test |
|---|---|
| FR-01, T01 | `test_t01_new_instrument_is_discovered_validated_indexed_and_answered_with_zero_clicks` |
| FR-02, scheduler compliance | `test_seven_midnights_create_one_logical_run_each_and_a_missed_day_catches_up`, `test_midnight_is_computed_in_asia_riyadh_not_utc`, `test_a_second_scheduler_cannot_start_a_concurrent_run`, `test_a_run_left_running_by_a_dead_worker_is_recovered_and_retried` |
| FR-03 | `test_fr03_agent_search_cannot_leave_the_official_allowlist`, `test_only_plain_https_urls_on_allowlisted_hosts_are_accepted`, `test_redirects_are_revalidated_on_every_hop` |
| FR-04 | snapshot assertions in T01 and `test_upload_review_approve_flow_over_http` |
| FR-06, T02 | `test_t02_identical_second_run_is_a_noop` |
| FR-07, T07 | `test_t07_drafts_and_press_releases_never_enter_the_authoritative_corpus` |
| FR-08, FR-11, T03–T05 | `test_t03_t04_t05_amendment_reembeds_only_changed_articles_and_keeps_history` |
| FR-09 (new law, amendment, repeal) | T01, T03, `test_repeal_is_published_from_its_evidenced_date` |
| FR-10, T08 | `test_t08_uncertain_changes_are_quarantined_alerted_and_other_updates_continue` |
| FR-12 | `test_fr12_rollback_restores_the_previous_generation_and_can_be_undone`, `test_failed_index_health_check_keeps_the_previous_generation` |
| FR-13, T10 | `test_t10_feed_outage_retries_reports_degraded_freshness_and_keeps_the_index`, `test_embedding_failure_publishes_nothing_and_is_retried_by_a_later_run` |
| FR-15, T06 | `test_t06_future_effective_amendment_is_published_as_scheduled_not_current` |
| FR-17 | `test_public_freshness_updates_and_chat_contract` |
| FR-18, FR-19 | `test_operator_endpoints_trigger_inspect_quarantine_and_roll_back` |
| T09 | `test_t09_instructions_inside_a_pdf_cannot_change_tools_allowlist_or_corpus`, `test_bounded_agent_proposals_are_verified_against_the_source` |
| Escape hatch (8.2) | `test_quarantined_candidate_can_be_corrected_through_the_escape_hatch` |

## Production verification (PRD §18)

1. Operations → source registry, corpus size, last verified sync.
2. Confirm that the official Umm Al-Qura source is enabled and no source is marked as test data.
3. Press **تشغيل المزامنة الآن** (or wait for midnight): the official feed is checked, complete
   official pages are fetched, and eligible changes are incrementally indexed.
4. Ask a question covered by a published item: the answer cites its exact official page; an earlier
   as-of date still resolves the version that was effective then.
5. Inspect quarantined items and their deterministic reason codes; they never affect live answers.
6. Run again for idempotence (`no_op`), then verify the active generation from Operations.
