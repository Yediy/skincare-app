"""Direct unit coverage of app.security.tokens, added alongside the
python-jose -> PyJWT swap (ecdsa, a hard, unfixed-CVE dependency of
python-jose, was pulled in even though this app only ever uses
HS256). No behavior here changed by that swap -- these tests exist to
prove it, and to pin down the token contract PyJWT must keep
honoring: sub/family_id/iat/exp/type claims, expiry, signature
tampering, and algorithm-confusion rejection.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.security.tokens import JWTError, create_access_token, decode_access_token


def test_round_trip_carries_the_expected_claims():
    token = create_access_token(user_id="user-1", family_id="family-1")
    payload = decode_access_token(token)

    assert payload["sub"] == "user-1"
    assert payload["family_id"] == "family-1"
    assert payload["type"] == "access"
    assert "iat" in payload
    assert "exp" in payload
    assert payload["exp"] > payload["iat"]


def test_expired_token_is_rejected():
    now = datetime.now(timezone.utc)
    expired_payload = {
        "sub": "user-1",
        "family_id": "family-1",
        "iat": now - timedelta(minutes=30),
        "exp": now - timedelta(minutes=15),
        "type": "access",
    }
    expired_token = jwt.encode(expired_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    with pytest.raises(JWTError):
        decode_access_token(expired_token)


def test_tampered_signature_is_rejected():
    token = create_access_token(user_id="user-1", family_id="family-1")
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}zzzz"

    with pytest.raises(JWTError):
        decode_access_token(tampered)


def test_token_signed_with_a_different_secret_is_rejected():
    now = datetime.now(timezone.utc)
    forged_payload = {
        "sub": "user-1",
        "family_id": "family-1",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "type": "access",
    }
    forged_token = jwt.encode(forged_payload, "a-completely-different-secret", algorithm=settings.jwt_algorithm)

    with pytest.raises(JWTError):
        decode_access_token(forged_token)


def test_algorithm_confusion_is_rejected():
    """A token signed with an algorithm other than the configured one
    (HS256) must be refused outright -- decode_access_token passes an
    explicit `algorithms=` allowlist rather than trusting the token's
    own `alg` header, which is what actually prevents algorithm
    confusion (e.g. a verifier that would otherwise accept "none", or
    accept an asymmetric algorithm's public key as an HMAC secret)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "user-1",
        "family_id": "family-1",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "type": "access",
    }
    other_alg_token = jwt.encode(payload, settings.jwt_secret, algorithm="HS512")

    with pytest.raises(JWTError):
        decode_access_token(other_alg_token)


def test_wrong_token_type_is_rejected():
    now = datetime.now(timezone.utc)
    refresh_shaped_payload = {
        "sub": "user-1",
        "family_id": "family-1",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "type": "refresh",
    }
    token = jwt.encode(refresh_shaped_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    with pytest.raises(JWTError):
        decode_access_token(token)
