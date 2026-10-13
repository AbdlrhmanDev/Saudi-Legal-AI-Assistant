# Saudi Legal AI Assistant

An Arabic, source-grounded legal RAG application for reviewed Saudi regulatory documents. It combines temporal retrieval, article-level citations, a human approval workflow, monitored updates, and explicit abstention when the indexed evidence is insufficient.

<p align="center">
  <img src="docs/assets/architecture.svg" alt="Saudi Legal AI Assistant architecture" width="100%">
</p>

> **Assessment scope:** this is an engineering MVP and legal-information tool, not legal advice. The repository includes a clearly labelled fictional corpus for repeatable demonstrations; it does not claim complete coverage of Saudi law.

## What is implemented

| Capability | Status |
|---|---|
| Arabic parsing, immutable document versions and effective-date intervals | Implemented |
| PostgreSQL full-text + pgvector retrieval with reciprocal-rank fusion | Implemented |
| Evidence gating, article citations, answer validation and abstention | Implemented |
| Source monitoring, diff, reviewer approval/rejection and rollback | Implemented |
| Arabic React UI for chat, corpus, review and operations | Implemented |
| Persistent chat history | Implemented in browser storage; last 50 completed messages, with a clear-chat action |
| Authentication, audit events, traces, worker heartbeat and health checks | Implemented |
| Real production corpus and production email delivery | Deliberately not bundled; see [limitations](#known-limitations) |

Latest verified local run (13 October 2026): **137 backend tests passed**, Python lint passed, frontend lint and production build passed. The 34-case offline fixture evaluation measured 100% Recall@5, 100% citation accuracy, 96.2% auto-checked correctness, 100% abstention and 100% temporal scenarios. See the generated [evaluation report](tests/evaluation/results/report.md) for scope and the single known paraphrase miss.

## System walkthrough

The screenshots below show the implemented Arabic RTL application and its real review workflow. All visible legal content is demo/test data and must not be treated as official legal advice.

### Arabic chat and grounded answers

<table>
  <tr>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/chat-insufficient-evidence.png" alt="Arabic legal chat refusing to invent an answer when the indexed evidence is insufficient" width="100%"><br>
      <sub><b>Safe abstention:</b> the assistant clearly refuses to state a legal rule when the indexed evidence is insufficient.</sub>
    </td>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/chat-grounded-answer.png" alt="Arabic RAG answer grounded in Labor Law article five with a visible citation" width="100%"><br>
      <sub><b>Grounded answer:</b> an in-corpus question returns the applicable rule, effective-date context and a citation to article 5.</sub>
    </td>
  </tr>
</table>

### Citations and source inspection

<p align="center">
  <img src="docs/assets/screenshots/citation-source-panel-clean.png" alt="Clean citation panel showing source status, publication date, effective date and compact legal text" width="72%">
</p>

<p align="center"><sub><b>Citation panel:</b> clean controlling text without copied page navigation or artificial spacing, alongside review status, publication date and effective date.</sub></p>

### Corpus dashboard and immutable versions

<p align="center">
  <img src="docs/assets/screenshots/corpus-overview.png" alt="Admin legal corpus dashboard with document counts and version timeline" width="100%">
</p>

<p align="center"><sub><b>Corpus dashboard:</b> reviewed documents, pending-review counts, duplicate warnings and immutable version timeline.</sub></p>

<table>
  <tr>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/version-timeline.png" alt="Document version timeline and reviewer actions" width="100%"><br>
      <sub><b>Version timeline:</b> current and pending versions with their effective-date state.</sub>
    </td>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/corpus-version-actions.png" alt="Document version actions for viewing editing and deletion" width="100%"><br>
      <sub><b>Reviewer controls:</b> view document, edit metadata, open official source or remove a draft.</sub>
    </td>
  </tr>
</table>

### Document review and official-source verification

<p align="center">
  <img src="docs/assets/screenshots/document-review.png" alt="Full document review screen with extracted official text and classification" width="100%">
</p>

<p align="center"><sub><b>Document review:</b> extracted Arabic text, source identity, content classification, affected instrument and temporal metadata.</sub></p>

<table>
  <tr>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/review-official-source.png" alt="Review gate requiring an official text attachment" width="100%"><br>
      <sub><b>Source gate:</b> an observed publication cannot be approved until its complete official text is attached.</sub>
    </td>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/review-decision.png" alt="Reviewer decision screen for an amendment" width="100%"><br>
      <sub><b>Review decision:</b> classification, effective-date evidence, source confirmation, approval and rejection.</sub>
    </td>
  </tr>
</table>

### Approval, publication and effective-date controls

<p align="center">
  <img src="docs/assets/screenshots/approval-form.png" alt="Approval form with publication and effective-date safeguards" width="82%">
</p>

<p align="center"><sub><b>Approval safeguards:</b> publication date and effective date remain separate; reviewer confirmations are required before indexing.</sub></p>

<table>
  <tr>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/approval-date-fields.png" alt="Publication date effective date and effective-date evidence fields" width="100%"><br>
      <sub><b>Temporal review:</b> explicit publication date, effective date and the legal evidence establishing applicability.</sub>
    </td>
    <td width="50%" align="center">
      <img src="docs/assets/screenshots/review-confirmation.png" alt="Final review confirmation before corpus publication" width="100%"><br>
      <sub><b>Final confirmation:</b> the reviewer confirms the controlling text before adding it to the corpus.</sub>
    </td>
  </tr>
</table>

### Approved state

<p align="center">
  <img src="docs/assets/screenshots/approved-version.png" alt="Approved legal document version and audit state" width="82%">
</p>

<p align="center"><sub><b>Approved version:</b> immutable review outcome, activation date and audit information after publication to the corpus.</sub></p>

## Five-minute evaluator setup

Requirements: Docker Desktop with Compose. No external AI key is required for the deterministic demo.

### Windows PowerShell

```powershell
Copy-Item .env.example .env
# Set ADMIN_PASSWORD to 12+ characters in .env
docker compose up --build -d
docker compose run --rm -e ALLOW_FIXTURE_SOURCES=true migrate python -m legal_core.cli bootstrap-demo
cd apps/web
npm install
npm run dev
```

### macOS / Linux

```bash
cp .env.example .env
# Set ADMIN_PASSWORD to 12+ characters in .env
docker compose up --build -d
docker compose run --rm -e ALLOW_FIXTURE_SOURCES=true migrate python -m legal_core.cli bootstrap-demo
cd apps/web
npm install
npm run dev
```

Open:

- Web app: <http://localhost:5173/#app>
- Reviewer login: <http://localhost:5173/#login>
- API documentation: <http://localhost:8000/docs>
- Readiness probe: <http://localhost:8000/health/ready>

Use `ADMIN_EMAIL` and `ADMIN_PASSWORD` from `.env` for the reviewer account. If port `5432` is occupied, set both `POSTGRES_PORT` and the port in `DATABASE_URL`.

## Suggested evaluation flow

1. Ask `كم مدة فترة التجربة في نظام العمل؟` and open the numbered source citation.
2. Refresh the browser: the completed conversation remains available. Use **محادثة جديدة** to erase it locally.
3. Ask an out-of-corpus question and confirm the assistant abstains without inventing citations.
4. Sign in as the reviewer and inspect the corpus, version timeline, source link and effective date.
5. Run source sync twice; the simulated amendment remains one candidate because ingestion is idempotent.
6. Confirm the pending amendment does not affect answers, approve it with an effective date and evidence, then compare answers before and after that date.
7. Open Operations to inspect jobs, source status, worker health and audit events.

The complete workflow is automated in [`tests/integration/test_workflows.py`](tests/integration/test_workflows.py).

## How the RAG pipeline works

1. The query date and optional legal domain filter the corpus to **approved** versions effective on that date.
2. PostgreSQL Arabic lexical search and pgvector semantic search produce separate rankings.
3. Reciprocal-rank fusion combines the rankings; statute and article hints add deterministic signals.
4. An evidence gate rejects weak retrieval before any language model is called.
5. The answer provider receives only retrieved evidence identifiers and text.
6. The server validates citations and constructs source metadata from database rows, never from model output.
7. Invalid output is retried once, then replaced by a cited extractive fallback.

The default `hash` embedding and `extractive` answer providers are deterministic and work offline. Set `EMBEDDING_PROVIDER=openai`, `LLM_PROVIDER=openai`, and `OPENAI_API_KEY` to exercise the generative path. Re-index approved versions after changing an embedding model or provider.

## Review and update lifecycle

```text
allowlisted source → candidate → acquire/attach document → parse + diff
                   → reviewer verifies dates and warnings → approve or reject
                   → immutable version + index → date-aware RAG
```

Only approved versions enter retrieval. Approval requires a confirmed `effective_from` date and supporting evidence. Publication date is stored separately and is never silently used as the effective date. Rollback deactivates an index/version and appends an audit event; it does not delete history.

## Local development

Requires Python 3.11 or 3.12, [uv](https://docs.astral.sh/uv/) and PostgreSQL with pgvector 0.8+ (`docker compose up -d db` provides the database).

```bash
uv sync
uv run alembic upgrade head
uv run python -m legal_core.cli bootstrap
uv run uvicorn api.main:app --reload
uv run python -m worker.main
```

In another terminal:

```bash
cd apps/web
npm install
npm run dev
```

`VITE_API_URL` defaults to `http://localhost:8000`. If it changes, add the frontend origin to `CORS_ORIGINS`.

## Verification

Backend quality and test suite:

```bash
uv run ruff check .
uv run pytest -q
```

Frontend quality and production build:

```bash
cd apps/web
npm run lint
npm run build
```

Evaluation harness:

```bash
uv run python -m tests.evaluation.run_eval
# Uses providers configured in .env:
uv run python -m tests.evaluation.run_eval --live
```

Integration tests need a PostgreSQL database whose name ends in `_test`, or an explicit `TEST_DATABASE_URL`. The harness rebuilds only that test database. The latest fixture-corpus report is in [`tests/evaluation/results/report.md`](tests/evaluation/results/report.md); it is a regression result, not a claim about performance on all Saudi legislation.

The PRD demonstration cases are covered as follows:

| Case | Automated verification |
|---|---|
| Cited answer | `test_q1_answer_carries_an_article_level_citation_and_trace` |
| Out-of-corpus abstention | `test_q2_out_of_corpus_question_abstains_without_citations` |
| Different answers by date | `test_q6_q3_approval_changes_time_valid_answers_and_is_audited` |
| Idempotent feed reads | `test_q4_repeated_feed_reads_create_exactly_one_candidate` |
| Unapproved update is invisible | `test_q5_unapproved_update_never_reaches_answers` |
| Approval indexes and audits | `test_q6_q3_approval_changes_time_valid_answers_and_is_audited` |
| Hostile document instructions | `test_q7_instructions_inside_documents_are_not_followed` |
| Source outage | `test_q8_source_outage_is_retried_reported_and_harmless` |

## Privacy and security defaults

- Question text is not stored server-side unless `TRACE_STORE_QUERY_TEXT=true`; traces retain a query hash and retrieved/cited identifiers.
- Browser chat history is local to that browser profile, capped at 50 completed messages and removable from the chat header.
- Source fetching is restricted by `SOURCE_ALLOWLIST`; redirects are checked again.
- Uploaded HTML has active content removed. Instruction-like legal passages are quarantined from evidence and require reviewer acknowledgement.
- Admin endpoints require JWT authentication; secrets belong in `.env`, which is ignored by Git.
- Rate limits, upload size/type validation, audit logging and worker-readiness checks are enabled.

See [`docs/source_policy.md`](docs/source_policy.md) for source governance and [`docs/architecture.md`](docs/architecture.md) for data invariants and retrieval details.

## Configuration

See [`.env.example`](.env.example). Important variables:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER`, `LLM_MODEL` | `extractive` for offline reproducibility or `openai` for grounded generation |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIM` | `hash` or `openai`; model changes require re-indexing |
| `SOURCE_ALLOWLIST` | Hosts allowed for monitored acquisition |
| `ALLOW_FIXTURE_SOURCES` | Explicit opt-in for fictional demo data |
| `TRACE_STORE_QUERY_TEXT`, `TRACE_RETENTION_DAYS` | Trace privacy and retention |
| `MIN_LEXICAL_COVERAGE`, `MIN_VECTOR_SIMILARITY` | Retrieval evidence thresholds |

## Repository layout

```text
apps/web/              React + TypeScript Arabic UI
packages/legal_core/   Parsing, ingestion, temporal rules, retrieval and answer pipeline
services/api/          FastAPI routes, schemas and authentication
services/worker/       PostgreSQL job runner, scheduler and heartbeat
db/migrations/         Alembic schema migrations
fixtures/              Fictional, checksum-pinned demo corpus and simulated feed
tests/                 Unit, integration, security workflows and evaluation harness
docs/                  Architecture, source policy and API notes
```

## Known limitations

- The bundled corpus is fictional and intentionally small. Official sources are registered disabled until an administrator completes source/licence review.
- The deterministic hash embedding provider is useful for repeatability, not semantic-quality claims. A real embedding model is needed for paraphrases with little shared vocabulary.
- OpenAI generation is implemented but requires the evaluator's own API key and a live evaluation run.
- Scanned PDFs requiring OCR are rejected with a clear error and are outside this MVP.
- Amendments containing only changed clauses are flagged for review rather than automatically merged into a consolidated law.
- Password-reset email delivery, distributed rate limiting and production TLS termination are deployment responsibilities.
- A PDPL/cross-border data-transfer assessment is required before handling real user questions in production.

## Further documentation

- [Architecture and invariants](docs/architecture.md)
- [Source governance](docs/source_policy.md)
- [Frontend/API integration](docs/api.md)
- [Evaluation report](tests/evaluation/results/report.md)
