import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from jwt import InvalidTokenError as JWTError

from app.config import settings


def create_access_token(user_id: str, family_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "family_id": family_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    # algorithms= is an allowlist, not a hint -- PyJWT refuses to decode a
    # token signed with anything outside it, which is what actually blocks
    # algorithm-confusion attacks (e.g. a token claiming "alg: none", or an
    # RS256-signed token replayed against an HS256 verifier using the
    # public key as the HMAC secret).
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != "access":
        raise JWTError("Not an access token")
    return payload


def generate_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
