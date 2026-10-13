# Railway deployment

This repository deploys to one Railway project as three services:

1. `Postgres` — use Railway's **pgvector** template.
2. `legal-api` — API and background worker from the repository root.
3. `legal-web` — React/Vite frontend with root directory `/apps/web`.

The API and worker intentionally share one service because uploaded documents
are stored on a Railway volume. A volume cannot be shared between independent
services, while the worker must be able to read files accepted by the API.

## `legal-api`

- Source: this GitHub repository, branch `main`
- Dockerfile: `/Dockerfile`
- Start command: `python deploy/railway/run_api_and_worker.py`
- Pre-deploy command: `alembic upgrade head && python -m legal_core.cli bootstrap-demo`
- Health check: `/health/ready`
- Health-check timeout: `300`
- Public networking: enabled
- Volume mount: `/app/var/storage`

Variables:

```dotenv
DATABASE_URL=${{Postgres.DATABASE_URL}}
ENVIRONMENT=production
JWT_SECRET=<random-secret-of-at-least-32-characters>
ADMIN_EMAIL=<private-evaluator-email>
ADMIN_PASSWORD=<private-password-of-at-least-12-characters>
CORS_ORIGINS=https://${{legal-web.RAILWAY_PUBLIC_DOMAIN}}
EMBEDDING_PROVIDER=hash
EMBEDDING_DIM=1536
LLM_PROVIDER=extractive
ALLOW_FIXTURE_SOURCES=true
TRACE_STORE_QUERY_TEXT=false
STORAGE_DIR=/app/var/storage
```

The demo providers are deterministic and do not require an OpenAI API key.
The seeded corpus is fictional test data and is labelled as such in the UI.

## `legal-web`

- Source: this GitHub repository, branch `main`
- Root directory: `/apps/web`
- Dockerfile: `/apps/web/Dockerfile`
- Public networking: enabled

Variable:

```dotenv
VITE_API_URL=https://${{legal-api.RAILWAY_PUBLIC_DOMAIN}}
```

After both domains exist, redeploy both services so the frontend API URL and
backend CORS origin are rebuilt with the final Railway domains.

## Smoke test

1. Open `https://${{legal-api.RAILWAY_PUBLIC_DOMAIN}}/health/ready` and confirm
   the database, pgvector, and worker checks are healthy.
2. Open the web domain and sign in with the private admin credentials.
3. Verify the corpus contains the fictional labor, companies, and privacy laws.
4. Ask a grounded question and an out-of-scope question.
5. Upload a small text document and verify that its processing job completes.

Never commit production secrets or publish evaluator credentials in README.
