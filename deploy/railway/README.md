# Railway deployment

Recommended layout for 1,000+ users (four services in one Railway project):

1. `Postgres` — Railway's **pgvector** template.
2. `legal-api` — stateless API, **2+ replicas**, from the repository root.
3. `legal-worker` — nightly synchronization + job queue, **exactly 1 replica**, same image.
4. `legal-web` — React/Vite frontend with root directory `/apps/web`.

Uploaded and fetched documents are stored as immutable snapshots in PostgreSQL, so no volume is
needed and the API can scale horizontally. Only the worker runs the 00:00 Asia/Riyadh schedule;
the advisory lock and the per-day run key keep one logical run per day even if a second worker
starts by accident.

## Shared variables (both `legal-api` and `legal-worker`)

```dotenv
DATABASE_URL=${{Postgres.DATABASE_URL}}
ENVIRONMENT=production
JWT_SECRET=<random-secret-of-at-least-32-characters>
ADMIN_EMAIL=<private-evaluator-email>
ADMIN_PASSWORD=<private-password-of-at-least-12-characters>
CORS_ORIGINS=https://${{legal-web.RAILWAY_PUBLIC_DOMAIN}}
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=<private-openai-api-key>
ALLOW_FIXTURE_SOURCES=false
TRACE_STORE_QUERY_TEXT=false
SYNC_TIMEZONE=Asia/Riyadh
SYNC_HOUR=0
SYNC_MINUTE=0
# Connection budget: (DB_POOL_SIZE + DB_MAX_OVERFLOW) x WEB_CONCURRENCY x replicas + worker
# must stay below Postgres max_connections (Railway default 100). See docs/scaling.md.
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=5
```

## `legal-api`

- Dockerfile: `/Dockerfile` (default start command: uvicorn with `WEB_CONCURRENCY` processes)
- Replicas: 2 (raise with load; the API holds no state)
- Pre-deploy command: `alembic upgrade head && python -m legal_core.cli bootstrap && python -m legal_core.cli reindex-approved`
- Health check: `/readyz` (database + pgvector; a stalled worker does not take the API offline)
- Public networking: enabled
- Extra variables:

```dotenv
WEB_CONCURRENCY=2
# Railway's edge proxy appends the real client address to X-Forwarded-For. Without this every
# user would share the proxy's address and therefore one rate-limit bucket.
TRUSTED_PROXY_COUNT=1
RATE_LIMIT_BACKEND=database
RATE_LIMIT_CHAT_PER_MINUTE=20
```

## `legal-worker`

- Dockerfile: `/Dockerfile`
- Start command: `python -m worker.main`
- Replicas: **1**
- Public networking: disabled
- Optional: `ALERT_WEBHOOK_URL=<Slack/Teams incoming webhook>` for newly published laws,
  quarantine and outage alerts. Successful no-change runs stay silent.

## `legal-web`

- Root directory: `/apps/web`, Dockerfile `/apps/web/Dockerfile`, public networking enabled

```dotenv
VITE_API_URL=https://${{legal-api.RAILWAY_PUBLIC_DOMAIN}}
```

After both domains exist, redeploy both services so the frontend API URL and backend CORS origin
are rebuilt with the final Railway domains.

If an older demo deployment already contains fixture data, run this once in the API service shell:

```bash
python -m legal_core.cli purge-test-data
```

It deletes only rows explicitly marked as test data, removes unsafe historical index manifests,
and activates a fresh official-only RAG generation.

After enabling OpenAI embeddings, or whenever the embedding provider/model/dimension changes, run
this once in the API service shell before serving questions:

```bash
python -m legal_core.cli reindex-approved
```

This atomically replaces the old surface-overlap vectors with semantic vectors for every approved
version and activates the new index only after its health checks pass.

## Small single-service mode

For a demo you can still run API and worker in one service with
`python deploy/railway/run_api_and_worker.py` (keep that service at 1 replica).

## Smoke test

1. `https://${{legal-api.RAILWAY_PUBLIC_DOMAIN}}/readyz` returns `ready`; `/api/v1/freshness` shows
   the next scheduled sync at `T00:00:00+03:00`.
2. Sign in as admin, open **Operations**, press **Run sync now** and watch the run finish.
3. Ask a grounded question and an out-of-scope question; both show the freshness line.
4. Run the load test in `tests/load/README.md` against a staging deployment.

Never commit production secrets or publish evaluator credentials in README.
