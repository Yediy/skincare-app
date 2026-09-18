"""Provider-neutral transactional email boundary (V1 account recovery
pass, Part 3). Application/domain code depends on
TransactionalEmailService, never on Resend (or any other provider's
SDK/HTTP API) directly -- ResendTransactionalEmailService is the only
module in this codebase allowed to talk to Resend, the same discipline
app/storage/base.py's own docstring establishes for
ObjectStorage/CloudflareR2ObjectStorage. A future SES/Postmark adapter
would implement this same interface without
PasswordResetService/app.main changing at all.

Nothing in this module logs, or lets a caller log through it, the
recipient email or the reset URL it's asked to send -- see
ACCOUNT_RECOVERY_ARCHITECTURE.md's "what must never be logged" section.
"""
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


class TransactionalEmailError(Exception):
    """Base class for every error this abstraction raises. Callers
    should be able to catch this without knowing which provider is
    behind it -- a provider adapter must translate its own SDK/HTTP
    errors into this, not let them leak through."""


class TransactionalEmailService(ABC):
    @abstractmethod
    async def send_password_reset(self, *, to_email: str, reset_url: str, idempotency_key: str) -> None:
        """Sends a password-reset email whose body links to
        `reset_url` (itself carrying the one-time plaintext reset
        token as a query parameter -- see PASSWORD_RESET_URL_BASE in
        app/config.py). Raises TransactionalEmailError on failure;
        never raises for "this recipient doesn't exist" (that
        decision belongs to PasswordResetService, upstream of this
        interface, which never calls this method for an ineligible
        account in the first place).

        `idempotency_key` is the caller's stable delivery identity --
        app/workers/password_reset_email_worker.py derives it from the
        durable job's own UUID (`f"password-reset/{job.id}"`), never a
        value generated per attempt, so every reclaim/retry of the same
        job presents the exact same key. Queue-level at-least-once
        delivery alone cannot undo an email a provider already sent
        (unlike a DB write, there is no local transaction to roll
        back), so this key exists to push that same idempotency
        guarantee out to the provider itself -- an adapter that
        actually talks to a provider (ResendTransactionalEmailService)
        must forward it as that provider's own idempotency mechanism.
        Implementations must never derive a key from `to_email` or
        `reset_url` themselves (those belong in the request body, not
        the identity used to deduplicate it) and never fall back to
        generating one of their own."""
        raise NotImplementedError


class ResendTransactionalEmailService(TransactionalEmailService):
    """Real implementation, talking to Resend's HTTP API
    (https://api.resend.com/emails). `transport` is accepted purely
    for testing (httpx.MockTransport) -- production code never passes
    it, the same pattern RevenueCatAPIClient
    (app/domain/revenuecat_reconciliation_service.py) already
    establishes for this codebase's other outbound HTTP integration."""

    _API_URL = "https://api.resend.com/emails"

    def __init__(
        self, *, api_key: str, from_email: str, timeout_seconds: float = 10.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self._api_key = api_key
        self._from_email = from_email
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def send_password_reset(self, *, to_email: str, reset_url: str, idempotency_key: str) -> None:
        payload = {
            "from": self._from_email,
            "to": [to_email],
            "subject": "Reset your password",
            "text": (
                "We received a request to reset your password.\n\n"
                f"Reset it here: {reset_url}\n\n"
                "This link expires soon. If you didn't request this, you can safely ignore this email."
            ),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Resend's own retry-safe delivery mechanism
            # (https://resend.com/docs/api-reference/emails/send-email#idempotency-key):
            # a worker that reclaims the SAME durable job after a
            # crash/lease-loss between "Resend accepted this" and this
            # process's own acknowledge() replays this exact header, so
            # Resend itself recognizes the retry and returns the
            # original send rather than dispatching a second email --
            # the one thing queue-level (Postgres) idempotency cannot
            # do once the provider has already accepted a request.
            "Idempotency-Key": idempotency_key,
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
            try:
                response = await client.post(self._API_URL, json=payload, headers=headers)
            except httpx.HTTPError as e:
                # Deliberately no `to_email`/`reset_url`/payload in
                # this message -- see the module docstring.
                raise TransactionalEmailError(f"{e.__class__.__name__} contacting Resend") from e
        if response.status_code >= 400:
            raise TransactionalEmailError(f"Resend returned HTTP {response.status_code}")


class NullTransactionalEmailService(TransactionalEmailService):
    """Explicit no-op adapter -- the default for EMAIL_PROVIDER=none
    (development/CI without a configured provider). Logs one
    structured, non-sensitive event per call (no email address, no
    URL, no token) so "no email was actually sent" is visible in logs
    rather than silently indistinguishable from a real send.

    app/config.py's production validation refuses to start with
    EMAIL_PROVIDER anything other than "resend" -- this class can
    never become the production provider merely by an operator leaving
    EMAIL_PROVIDER unset, unlike a bare pass-through no-op would."""

    async def send_password_reset(self, *, to_email: str, reset_url: str, idempotency_key: str) -> None:
        logger.info("password_reset_email_skipped: EMAIL_PROVIDER=none, no email was sent")


class InMemoryTransactionalEmailService(TransactionalEmailService):
    """Test fake -- records every call in memory, never performs any
    real network I/O. The only email implementation any test in this
    repository may construct; build_transactional_email_service()
    below never returns this, so production code can never receive it
    either."""

    def __init__(self):
        self.sent: List[dict] = []

    async def send_password_reset(self, *, to_email: str, reset_url: str, idempotency_key: str) -> None:
        self.sent.append({"to_email": to_email, "reset_url": reset_url, "idempotency_key": idempotency_key})


def build_transactional_email_service() -> TransactionalEmailService:
    """Composition-root factory (app/main.py), mirroring
    build_entitlement_service's own shape (app/domain/entitlement.py):
    every real caller asks for "the current TransactionalEmailService"
    through this one function, so flipping settings.email_provider is
    the only change needed anywhere. app/config.py's own
    _reject_unsafe_production_config already fails application startup
    outright if EMAIL_PROVIDER=resend in production without a real,
    non-placeholder RESEND_API_KEY/PASSWORD_RESET_FROM_EMAIL/
    PASSWORD_RESET_URL_BASE configured -- this function does not
    re-validate any of that, it only decides which concrete class to
    construct."""
    from app.config import settings

    if settings.email_provider == "resend":
        return ResendTransactionalEmailService(
            api_key=settings.resend_api_key or "",
            from_email=settings.password_reset_from_email or "",
        )
    return NullTransactionalEmailService()
