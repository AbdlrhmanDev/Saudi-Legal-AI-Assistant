import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import func, select, update

from api import serializers
from api.deps import ApiError, CurrentUser, Db, SettingsDep, client_key, ok, rate_limiter
from api.schemas import ForgotRequest, LoginRequest, RegisterRequest, ResetRequest
from api.security import create_access_token, hash_password, verify_password
from legal_core.config import Settings
from legal_core.models import PasswordResetToken, User, utcnow

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = logging.getLogger("api.auth")


def _session(user: User, settings: Settings) -> dict:
    return {
        "access_token": create_access_token(user, settings),
        "token_type": "bearer",
        "expires_in": settings.jwt_ttl_minutes * 60,
        "user": serializers.user(user),
    }


def _by_email(db: Db, email: str) -> User | None:
    return db.scalars(select(User).where(func.lower(User.email) == email.strip().lower())).first()


@router.post("/login", summary="Exchange credentials for a bearer token")
def login(body: LoginRequest, request: Request, db: Db, settings: SettingsDep) -> dict:
    rate_limiter.check(client_key(request, "login"), settings.rate_limit_login_per_minute)
    user = _by_email(db, body.email)
    if not verify_password(body.password, user.password_hash if user else None) or user is None:
        raise ApiError(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")
    user.last_login_at = utcnow()
    return ok(_session(user, settings))


@router.post("/register", status_code=201, summary="Create a read-only account")
def register(body: RegisterRequest, request: Request, db: Db, settings: SettingsDep) -> dict:
    """Self-service accounts always get the `user` role. Reviewer accounts are created by an administrator."""
    rate_limiter.check(client_key(request, "login"), settings.rate_limit_login_per_minute)
    if _by_email(db, body.email) is not None:
        raise ApiError(409, "EMAIL_TAKEN", "An account with this email already exists.")
    user = User(
        email=body.email.strip().lower(),
        name=body.name,
        role="user",
        password_hash=hash_password(body.password),
        last_login_at=utcnow(),
    )
    db.add(user)
    db.flush()
    return ok(_session(user, settings), "Account created.")


@router.post("/forgot", summary="Request a password-reset token")
def forgot(body: ForgotRequest, request: Request, db: Db, settings: SettingsDep) -> dict:
    """Always answers the same way, so the response does not reveal whether the email is registered."""
    rate_limiter.check(client_key(request, "login"), settings.rate_limit_login_per_minute)
    data: dict = {"sent": True}
    user = _by_email(db, body.email)
    if user is not None:
        now = utcnow()
        db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        )
        token = secrets.token_urlsafe(32)
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(minutes=settings.password_reset_ttl_minutes),
            )
        )
        # No mail service is configured: delivery is the deployment's job. The token is never logged.
        log.info("password_reset_requested", extra={"user_id": str(user.id)})
        if settings.expose_reset_token:
            data["dev_reset_token"] = token
    return ok(data, "If the email is registered, a reset link has been issued.")


@router.post("/reset", summary="Set a new password with a reset token")
def reset(body: ResetRequest, request: Request, db: Db, settings: SettingsDep) -> dict:
    rate_limiter.check(client_key(request, "login"), settings.rate_limit_login_per_minute)
    now = utcnow()
    record = db.scalars(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == hashlib.sha256(body.token.encode()).hexdigest())
        .with_for_update()
    ).first()
    if record is None or record.used_at is not None or record.expires_at <= now:
        raise ApiError(422, "INVALID_RESET_TOKEN", "The reset link is invalid or has expired.")
    user = db.get(User, record.user_id)
    record.used_at = now
    user.password_hash = hash_password(body.password)
    user.password_changed_at = now  # tokens issued earlier stop working
    return ok({"reset": True}, "Password changed.")


@router.get("/me", summary="Current user")
def me(user: CurrentUser) -> dict:
    return ok(serializers.user(user))
