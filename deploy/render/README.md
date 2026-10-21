# Render deployment

`render.yaml` provisions a free evaluator deployment with:

- one Docker web service serving the React UI and FastAPI on the same origin;
- the job worker and midnight Asia/Riyadh scheduler in the same instance;
- one Render Postgres 16 database with `pgvector` enabled by Alembic.

Create a Render Blueprint from the repository and provide `ADMIN_EMAIL`, `ADMIN_PASSWORD` (12+
characters), and `OPENAI_API_KEY` when prompted. Secrets are never committed.

The service runs migrations and the idempotent production bootstrap before each start. The free web
service sleeps after inactivity and catches up a missed nightly run after it wakes. Free Render
Postgres expires after 30 days and has no backups, so this layout is for assessment/demo use only.

On startup, the demo also imports any missing approved baseline instruments from their current
official Bureau of Experts pages (currently Traffic Law and Anti-Concealment Law). Existing active
versions are skipped, and a temporary source failure is retried on a later start without preventing
the API from coming online.

For sustained production, upgrade Postgres and split the API and worker into separate paid services.
