import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.deps import ApiError
from api.routes import admin, auth, chat, health, laws
from legal_core.config import get_settings
from legal_core.logconfig import configure_logging

log = logging.getLogger("api")

_HTTP_CODES = {400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": {"code": code, "message": message, "details": details or {}}}, status_code=status
    )


def create_app() -> FastAPI:
    settings = get_settings()
    settings.assert_safe_for_environment()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Saudi Legal AI Assistant API",
        version="0.1.0",
        description=(
            "Source-grounded legal information over a curated, reviewed corpus. "
            "This is an information service for a technical assessment, not legal advice."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum() else str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if settings.environment == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,  # path only: query strings and bodies are never logged
                "status": response.status_code,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )
        return response

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in err["loc"][1:]) or str(err["loc"][0]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return _error(422, "VALIDATION_ERROR", "The request is invalid.", {"fields": fields})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, _HTTP_CODES.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, _exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", extra={"request_id": getattr(request.state, "request_id", None)})
        return _error(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    for module in (health, auth, chat, laws, admin):
        app.include_router(module.router)
    return app


app = create_app()
