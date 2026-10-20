import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.security import decode_access_token
from legal_core.config import Settings, get_settings
from legal_core.db import get_sessionmaker
from legal_core.embeddings import Embedder, get_embedder
from legal_core.llm import AnswerModel, get_answer_model
from legal_core.models import RateLimitCounter, User

log = logging.getLogger("api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def ok(data: Any, message: str | None = None) -> dict:
    body = {"success": True, "data": data}
    if message:
        body["message"] = message
    return body


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@lru_cache
def _embedder() -> Embedder:
    return get_embedder(get_settings())


@lru_cache
def _answer_model() -> AnswerModel:
    return get_answer_model(get_settings())


def get_embedder_dep() -> Embedder:
    return _embedder()


def get_answer_model_dep() -> AnswerModel:
    return _answer_model()


Db = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder_dep)]
AnswerModelDep = Annotated[AnswerModel, Depends(get_answer_model_dep)]

_bearer = HTTPBearer(auto_error=False)


def optional_user(
    db: Db, settings: SettingsDep, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> User | None:
    if credentials is None:
        return None
    try:
        claims = decode_access_token(credentials.credentials, settings)
        user = db.get(User, uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, ValueError):
        raise ApiError(401, "INVALID_TOKEN", "The access token is invalid or expired.") from None
    if user is None:
        raise ApiError(401, "INVALID_TOKEN", "The access token is invalid or expired.")
    if user.password_changed_at and claims.get("iat", 0) < int(user.password_changed_at.timestamp()):
        raise ApiError(401, "INVALID_TOKEN", "The access token is invalid or expired.")
    return user


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise ApiError(401, "UNAUTHENTICATED", "Authentication is required.")
    return user


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    # The role is read from the database on every request, not trusted from the token.
    if user.role != "admin":
        raise ApiError(403, "FORBIDDEN", "Administrator role is required.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
Admin = Annotated[User, Depends(require_admin)]


class Page:
    def __init__(
        self,
        page: Annotated[int, Query(ge=1)] = 1,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> None:
        self.page, self.limit = page, limit

    def run(self, db: Session, stmt: Select, serialize: Callable[[Any], dict], scalars: bool = True) -> dict:
        total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        paged = stmt.limit(self.limit).offset((self.page - 1) * self.limit)
        rows = db.scalars(paged).all() if scalars else db.execute(paged).all()
        return {
            "items": [serialize(r) for r in rows],
            "pagination": {
                "page": self.page,
                "limit": self.limit,
                "total": total,
                "totalPages": math.ceil(total / self.limit) if total else 0,
            },
        }


PageDep = Annotated[Page, Depends()]


class RateLimiter:
    """Per-client request limits.

    RATE_LIMIT_BACKEND=database (default): fixed one-minute windows counted in an UNLOGGED table, so
    every API process and replica enforces one shared limit. If the database is unreachable the
    limiter falls back to per-process counting rather than failing open.
    RATE_LIMIT_BACKEND=memory: in-process sliding window (single process only)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> None:
        if get_settings().rate_limit_backend == "database":
            try:
                count = self._database_hit(key, window)
            except Exception:
                log.warning("rate_limit_store_unavailable", exc_info=True)
            else:
                if count > limit:
                    raise ApiError(429, "RATE_LIMITED", "Too many requests. Please retry shortly.")
                return
        self._memory_hit(key, limit, window)

    def _database_hit(self, key: str, window: float) -> int:
        start = datetime.fromtimestamp(int(time.time() // window * window), UTC)
        stmt = insert(RateLimitCounter).values(bucket=key[:200], window_start=start, count=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["bucket", "window_start"], set_={"count": RateLimitCounter.count + 1}
        ).returning(RateLimitCounter.count)
        with get_sessionmaker()() as session:
            count = session.execute(stmt).scalar_one()
            session.commit()
        return count

    def _memory_hit(self, key: str, limit: int, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                raise ApiError(429, "RATE_LIMITED", "Too many requests. Please retry shortly.")
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
        if get_settings().rate_limit_backend == "database":
            with get_sessionmaker()() as session:
                session.execute(delete(RateLimitCounter))
                session.commit()


rate_limiter = RateLimiter()


def client_address(request: Request) -> str:
    """The caller's address. Behind TRUSTED_PROXY_COUNT reverse proxies it is read from
    X-Forwarded-For that many entries from the right (entries further left are client-supplied and
    could be forged); without proxies the socket peer is used."""
    hops = get_settings().trusted_proxy_count
    if hops > 0:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def client_key(request: Request, bucket: str) -> str:
    return f"{bucket}:{client_address(request)}"
