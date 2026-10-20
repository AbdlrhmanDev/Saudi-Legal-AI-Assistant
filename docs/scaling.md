# Scaling and stability (1,000+ users)

## What changed and why

| Problem in the MVP | Effect at scale | Fix |
|---|---|---|
| Rate limit keyed on `request.client.host` | Behind Railway/nginx every request comes from the proxy, so **all users shared one 20 req/min bucket** | `TRUSTED_PROXY_COUNT` reads the real client from `X-Forwarded-For` (right-most trusted hop; forged left-most entries are ignored) — `api/deps.py::client_address` |
| Rate limit counted in process memory | With 2+ processes/replicas each one allowed the full limit | `RATE_LIMIT_BACKEND=database`: one fixed-window counter per client in an `UNLOGGED` table shared by every process |
| DB session held for the whole chat request | A slow LLM call (up to 2 × 20 s) held a pooled connection; the default pool (5 + 10) capped the API at ~15 concurrent chats | `answer_question` commits after retrieval, so the connection returns to the pool before the model call |
| One uvicorn process, 40 threads | ~40 concurrent chats per container | `WEB_CONCURRENCY` processes per container, `API_THREAD_LIMIT` (default 100) threads per process |
| API and worker in one service with a shared volume | Adding API replicas also multiplied the scheduler (PRD v2 §6 forbids this) | Uploads and fetched documents are immutable snapshots in PostgreSQL (`source_snapshots`); API scales out, worker stays at one replica |
| Every question recomputed | Identical popular questions each cost an embedding + LLM call | Per-process LRU answer cache keyed by `(normalized question, domain, as-of, today, active index generation, models)`; a corpus update changes the generation id, so stale answers are never served |
| Readiness tied to the worker heartbeat | A stalled worker took every API replica out of rotation | `/readyz` checks only what the API needs (database, pgvector); worker state is reported, not gating |
| Full re-embedding on every update | Cost and time grow with corpus size | Content-hash reuse: only changed articles call the embedding model |

## Capacity estimate

Assumptions for 1,000 daily users: 20 % active in the peak hour, 5 questions each, bursts at 10× the
hourly average.

| Quantity | 1,000 DAU | 10,000 DAU |
|---|---|---|
| Peak chat rate (burst) | ~3 req/s | ~30 req/s |
| In-flight chats (6 s LLM latency) | ~18 | ~180 |
| DB connections busy (≈150 ms per chat) | < 1 | ~5 |
| API containers (2 processes × 100 threads) | 2 (for redundancy) | 2–3 |
| LLM tokens per minute (~3k tokens/answer) | ~0.5 M TPM | ~5 M TPM — needs a high OpenAI tier and the answer cache |

The database is not the bottleneck; the model provider's rate limits are. Watch for HTTP 429 from
OpenAI in the logs (`llm_failed`) and raise `LLM_MAX_RETRIES` only together with your provider limits.

## Database connection budget

`(DB_POOL_SIZE + DB_MAX_OVERFLOW) × WEB_CONCURRENCY × API replicas + worker pool < max_connections`

Example for Railway Postgres (`max_connections = 100`): API `5 + 5`, 2 processes, 3 replicas = 60;
worker `5 + 5` = 10; migrations/admin headroom = 30.

Beyond that, put PgBouncer (transaction pooling) in front of the **API** only and set
`DB_PGBOUNCER=true` there. The **worker must connect directly**: the nightly sync uses a session-level
advisory lock.

## Recommended topology

```
            ┌──────────── load balancer / Railway edge (TRUSTED_PROXY_COUNT=1) ────────────┐
            │                                                                              │
     legal-api × N  (stateless; WEB_CONCURRENCY=2; /readyz)              legal-web (static)
            │
       PgBouncer (optional, transaction mode)
            │
   PostgreSQL + pgvector  ◄──── legal-worker × 1 (00:00 Asia/Riyadh sync, job queue)
```

Manifests: `deploy/railway/README.md`, `deploy/k8s/` (Deployment + HPA + PDB for the API, a
single-replica worker, and an optional Kubernetes CronJob with `timeZone: Asia/Riyadh`).

## Load testing

`tests/load/chat_load.js` (k6) ramps to 150 concurrent users with think time and checks the PRD's
chat p95 ≤ 8 s. Run it against staging before each capacity change; see `tests/load/README.md`.
