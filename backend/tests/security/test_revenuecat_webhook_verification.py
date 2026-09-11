"""Unit tests for app/security/revenuecat_webhook.py -- Authorization
header + HMAC signature verification, entirely offline (no DB/Redis),
same style as tests/unit/test_access_tokens.py."""
import hashlib
import hmac
import time

import pytest

from app.config import settings
from app.security.revenuecat_webhook import WebhookVerificationError, verify_webhook_request

RAW_BODY = b'{"event": {"type": "INITIAL_PURCHASE", "id": "evt_1"}}'
SECRET = "test-only-webhook-signing-secret-never-used-outside-pytest-0123456789"
AUTH_VALUE = "test-only-webhook-auth-value-never-used-outside-pytest-0123456789"


def _sign(body: bytes, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", AUTH_VALUE)
    monkeypatch.setattr(settings, "revenuecat_webhook_signing_secret", SECRET)
    monkeypatch.setattr(settings, "revenuecat_webhook_signature_tolerance_seconds", 300)
    monkeypatch.setattr(settings, "environment", "development")


def test_valid_authorization_and_valid_hmac_accepted(configured):
    now = int(time.time())
    signature = _sign(RAW_BODY, SECRET, now)
    verify_webhook_request(RAW_BODY, AUTH_VALUE, signature)  # does not raise


def test_invalid_authorization_rejected(configured):
    now = int(time.time())
    signature = _sign(RAW_BODY, SECRET, now)
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, "wrong-value", signature)
    assert exc.value.code == WebhookVerificationError.INVALID_AUTHORIZATION


def test_missing_authorization_rejected(configured):
    now = int(time.time())
    signature = _sign(RAW_BODY, SECRET, now)
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, None, signature)
    assert exc.value.code == WebhookVerificationError.MISSING_AUTHORIZATION


def test_invalid_hmac_rejected(configured):
    now = int(time.time())
    bad_signature = _sign(RAW_BODY, "a-completely-different-secret", now)
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, AUTH_VALUE, bad_signature)
    assert exc.value.code == WebhookVerificationError.INVALID_SIGNATURE


def test_missing_hmac_rejected_in_production(monkeypatch):
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", AUTH_VALUE)
    monkeypatch.setattr(settings, "revenuecat_webhook_signing_secret", SECRET)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, AUTH_VALUE, None)
    assert exc.value.code == WebhookVerificationError.MISSING_SIGNATURE


def test_missing_authorization_rejected_in_production(monkeypatch):
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", AUTH_VALUE)
    monkeypatch.setattr(settings, "revenuecat_webhook_signing_secret", SECRET)
    monkeypatch.setattr(settings, "environment", "production")
    now = int(time.time())
    signature = _sign(RAW_BODY, SECRET, now)
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, None, signature)
    assert exc.value.code == WebhookVerificationError.MISSING_AUTHORIZATION


def test_stale_signature_rejected(configured):
    stale_timestamp = int(time.time()) - 3600  # 1 hour old, well past the 300s tolerance
    signature = _sign(RAW_BODY, SECRET, stale_timestamp)
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, AUTH_VALUE, signature)
    assert exc.value.code == WebhookVerificationError.STALE_SIGNATURE


def test_malformed_signature_rejected(configured):
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(RAW_BODY, AUTH_VALUE, "not-a-real-signature-header")
    assert exc.value.code == WebhookVerificationError.MALFORMED_SIGNATURE


def test_signature_is_verified_over_raw_body_bytes_not_a_reserialized_copy(configured):
    """Proves the signature is checked against the *exact* raw bytes:
    a semantically-identical but differently-formatted JSON body (extra
    whitespace) must fail verification even though `json.loads` would
    treat both bodies as equal."""
    now = int(time.time())
    signature = _sign(RAW_BODY, SECRET, now)
    reformatted_body = b'{"event": {"type": "INITIAL_PURCHASE",  "id": "evt_1"}}'  # extra space
    assert reformatted_body != RAW_BODY
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook_request(reformatted_body, AUTH_VALUE, signature)
    assert exc.value.code == WebhookVerificationError.INVALID_SIGNATURE
