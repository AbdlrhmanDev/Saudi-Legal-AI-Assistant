FROM node:22-alpine AS web-build

WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web ./

# The production bundle calls the API on the same Render origin.
ARG VITE_API_URL=""
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (pinned by uv.lock) so code changes do not rebuild this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY packages ./packages
COPY services ./services
COPY db ./db
COPY deploy ./deploy
COPY scripts ./scripts
COPY fixtures ./fixtures
COPY tests ./tests
COPY alembic.ini ./
COPY --from=web-build /web/dist /app/web-dist
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && mkdir -p /app/var/storage && chown -R app:app /app/var
USER app

EXPOSE 8000
# WEB_CONCURRENCY uvicorn processes per container (each with its own DB pool and thread pool).
# Scale out with more containers; run the worker (python -m worker.main) as one separate service.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout-keep-alive 15 --no-server-header"]
