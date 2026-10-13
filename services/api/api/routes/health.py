from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from api.deps import Db, SettingsDep
from legal_core.models import WorkerHeartbeat, utcnow

router = APIRouter(prefix="/health", tags=["operations"])


@router.get("/live", summary="Process is up")
def live() -> dict:
    return {"status": "ok"}


@router.get("/ready", summary="Database and worker availability")
def ready(db: Db, settings: SettingsDep) -> JSONResponse:
    checks = {"database": "down", "pgvector": "down", "worker": "down"}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
        if db.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")):
            checks["pgvector"] = "ok"
        last_seen = db.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))
        if last_seen and (utcnow() - last_seen).total_seconds() <= settings.worker_stale_seconds:
            checks["worker"] = "ok"
    except Exception:
        db.rollback()
    is_ready = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if is_ready else "not_ready", "checks": checks}, status_code=200 if is_ready else 503)
