"""RevenueCat webhook request verification -- Authorization header +
HMAC signature, per REVENUECAT_INTEGRATION_NOTES.md section 1.

Deliberately operates on raw request body *bytes*, never a parsed/
re-serialized JSON object -- HMAC verification must be computed over
exactly the bytes RevenueCat signed, and re-serializing (even
byte-identical-looking JSON) is a real, documented way to silently
break this. The caller (app/api/v2/webhooks.py) is responsible for
calling this before ever parsing the body as JSON.

Never logs the Authorization header value, the signing secret, or the
raw HMAC signature -- only a closed set of rejection-reason codes (see
WebhookVerificationError), consumed by
app/observability/events.py::revenuecat_webhook_rejected.
"""
import hashlib
import hmac
import re
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

_SIGNATURE_RE = re.compile(r"^t=(?P<t>\d+),v1=(?P<v1>[0-9a-f]+)$")


class WebhookVerificationError(Exception):
    """Raised by verify_webhook_request. `code` is always one of the
    closed set below -- safe to log, never derived from secret
    material."""

    MISSING_AUTHORIZATION = "MISSING_AUTHORIZATION"
    INVALID_AUTHORIZATION = "INVALID_AUTHORIZATION"
    MISSING_SIGNATURE = "MISSING_SIGNATURE"
    MALFORMED_SIGNATURE = "MALFORMED_SIGNATURE"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    STALE_SIGNATURE = "STALE_SIGNATURE"

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _verify_hmac(raw_body: bytes, signature_header: str, secret: str, now: datetime) -> None:
    match = _SIGNATURE_RE.match(signature_header.strip())
    if not match:
        raise WebhookVerificationError(WebhookVerificationError.MALFORMED_SIGNATURE)

    timestamp_str = match.group("t")
    provided_signature = match.group("v1")

    # Signed payload is "<timestamp>.<raw body bytes>" -- exactly as
    # documented, never the parsed-and-reserialized body.
    signed_payload = f"{timestamp_str}.".encode("utf-8") + raw_body
    expected_signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_signature, provided_signature):
        raise WebhookVerificationError(WebhookVerificationError.INVALID_SIGNATURE)

    age_seconds = abs(now.timestamp() - int(timestamp_str))
    if age_seconds > settings.revenuecat_webhook_signature_tolerance_seconds:
        raise WebhookVerificationError(WebhookVerificationError.STALE_SIGNATURE)


def verify_webhook_request(
    raw_body: bytes,
    authorization_header: Optional[str],
    signature_header: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> None:
    """Raises WebhookVerificationError on any failure; returns None on
    success. Enforcement is driven entirely by which secrets are
    actually configured (settings.revenuecat_webhook_auth /
    revenuecat_webhook_signing_secret) -- in production,
    Settings._reject_unsafe_production_config already refuses to start
    at all with REVENUECAT_BILLING_ENABLED=true and either blank, so
    the `is_production` branches below are defense in depth, not the
    only thing standing between an unconfigured production deployment
    and an unverified webhook."""
    now = now or datetime.now(timezone.utc)

    configured_auth = settings.revenuecat_webhook_auth
    if configured_auth:
        if not authorization_header:
            raise WebhookVerificationError(WebhookVerificationError.MISSING_AUTHORIZATION)
        if not hmac.compare_digest(authorization_header, configured_auth):
            raise WebhookVerificationError(WebhookVerificationError.INVALID_AUTHORIZATION)
    elif settings.is_production:
        raise WebhookVerificationError(WebhookVerificationError.MISSING_AUTHORIZATION)

    signing_secret = settings.revenuecat_webhook_signing_secret
    if signing_secret:
        if not signature_header:
            raise WebhookVerificationError(WebhookVerificationError.MISSING_SIGNATURE)
        _verify_hmac(raw_body, signature_header, signing_secret, now)
    elif settings.is_production:
        raise WebhookVerificationError(WebhookVerificationError.MISSING_SIGNATURE)
