from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from legal_core.config import Settings
from legal_core.models import User

_ALGORITHM = "HS256"
# Verified against when the account does not exist, so login timing does not reveal valid emails.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    candidate = password.encode("utf-8")[:72]
    if password_hash is None:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    return bcrypt.checkpw(candidate, password_hash.encode("ascii"))


def create_access_token(user: User, settings: Settings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM], options={"require": ["exp", "sub"]})
