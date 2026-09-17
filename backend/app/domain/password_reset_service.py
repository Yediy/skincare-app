"""V1 account-recovery orchestration: forgot-password issuance and
reset-password completion (see ACCOUNT_RECOVERY_ARCHITECTURE.md). The
only module that ties together password_reset_repository (Postgres),
the durable password-reset-email delivery job (Part 3's provider-
neutral email boundary is now consumed only by
app/workers/password_reset_email_worker.py, never from this request
path -- see below), and reset-token generation -- POST /password/forgot
and POST /password/reset in app/main.py call this, not the repository
or job queue directly, so the enumeration-safety and atomicity
invariants documented here live in exactly one place.

Independent-review timing-enumeration fix: this module used to `await`
TransactionalEmailService.send_password_reset(...) directly inside
request_reset(), which meant only an eligible account's request paid
outbound Resend network latency -- a much larger and more variable
signal than anything response-body enumeration-safety alone protects
against, and one an attacker could distinguish statistically across
many requests. request_reset() now performs only bounded local
Postgres work for either branch (an eligibility lookup, or that lookup
plus issuing a token and enqueueing a delivery job) and never awaits
network I/O of any kind -- see PASSWORD_RESET_EMAIL_JOB_TYPE below and
app/domain/timing_normalization.py for the mechanism that pads the
remaining small residual timing difference.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

import asyncpg

from app.db import password_reset_repository
from app.domain.timing_normalization import TimingNormalizer
from app.queue.base import JobQueue
from app.security.reset_delivery_crypto import encrypt_delivery_payload

logger = logging.getLogger(__name__)

# Consumed by app/workers/password_reset_email_worker.py, which
# redeclares this same literal locally rather than importing it --
# matching this codebase's existing convention for every other job
# type (app/domain/analysis_submission_service.py's ANALYSIS_JOB_TYPE /
# app/workers/analysis_worker.py, app/api/v2/webhooks.py's
# REVENUECAT_WEBHOOK_JOB_TYPE / app/workers/revenuecat_webhook_worker.py).
PASSWORD_RESET_EMAIL_JOB_TYPE = "password_reset_email"


def generate_reset_token() -> Tuple[str, str]:
    """Same shape as app.security.tokens.generate_refresh_token(): a
    cryptographically secure random raw token (secrets.token_urlsafe,
    not random/uuid4) plus its SHA-256 hex digest. Only the digest is
    ever persisted in password_reset_tokens; the raw value exists only
    transiently -- in this process's memory while building the reset
    URL, and (Fernet-encrypted, never plaintext) in the delivery job's
    payload until the worker consumes it -- and must never be logged,
    returned in any API response, or passed to
    app.observability.events."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_reset_token(raw)


def hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PasswordResetService:
    def __init__(
        self, pool: asyncpg.Pool, job_queue: JobQueue, *,
        url_base: str, token_ttl_minutes: int, delivery_encryption_key: str,
        timing_normalizer: TimingNormalizer,
    ):
        self._pool = pool
        self._job_queue = job_queue
        self._url_base = url_base
        self._token_ttl_minutes = token_ttl_minutes
        self._delivery_encryption_key = delivery_encryption_key
        self._timing_normalizer = timing_normalizer

    async def request_reset(self, email: str) -> None:
        """Always returns normally -- never raises, and never signals
        anything different, for a nonexistent email, a disabled
        account (is_active=false), or a deleted account (deleted_at
        set). POST /password/forgot's outward response is identical
        regardless of what this method actually did, which is exactly
        what makes account enumeration impossible through this
        endpoint's response BODY.

        The entire eligible-or-ineligible branch below runs inside
        `self._timing_normalizer.run(...)`, which selects its target
        response duration before this closure ever runs (see that
        class's own docstring) -- the TIMING of this method's return is
        therefore also independent of which branch actually executed,
        not just its return value."""

        async def _do_lookup_and_maybe_issue() -> None:
            eligible = await password_reset_repository.lookup_eligible_user_by_email(self._pool, email)
            if eligible is None or not eligible["is_active"] or eligible["deleted_at"] is not None:
                return
            await self._issue_token_and_enqueue_delivery(eligible["id"], email)

        await self._timing_normalizer.run(_do_lookup_and_maybe_issue)

    async def _issue_token_and_enqueue_delivery(self, user_id: UUID, email: str) -> None:
        """Issues the reset token and enqueues its delivery job
        atomically, in one Postgres transaction -- a token must never
        exist with no corresponding delivery job (the user would never
        learn it exists), and a delivery job must never exist pointing
        at a token that was never actually persisted. No network I/O
        happens here or anywhere else in this method; the outbound
        Resend call happens only in
        app/workers/password_reset_email_worker.py, entirely outside
        this request."""
        raw_token, token_hash = generate_reset_token()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self._token_ttl_minutes)
        reset_url = f"{self._url_base}?token={raw_token}"
        # The raw token/reset_url are encrypted before they ever reach
        # `jobs.payload` -- an ordinary JSONB column with no RLS
        # restricting who can SELECT it, unlike password_reset_tokens
        # itself. Only this ciphertext, plus the already-non-sensitive
        # token_hash (identical treatment to refresh_tokens.token_hash),
        # is ever persisted -- see app/security/reset_delivery_crypto.py.
        encrypted_delivery = encrypt_delivery_payload(
            {"to_email": email, "reset_url": reset_url}, self._delivery_encryption_key,
        )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await password_reset_repository.issue_reset_token(
                    self._pool, user_id, token_hash, expires_at, conn=conn,
                )
                await self._job_queue.enqueue(
                    PASSWORD_RESET_EMAIL_JOB_TYPE,
                    {"token_hash": token_hash, "encrypted_delivery": encrypted_delivery},
                    conn=conn,
                )

    async def reset_password(self, raw_token: str, new_password_hash: str) -> Optional[UUID]:
        """Returns the user_id whose password was actually changed, or
        None if the token was invalid/expired/already-used/unknown or
        its account is no longer eligible -- see
        password_reset_repository.consume_token_and_apply_reset for
        the atomic mechanism. The caller (POST /password/reset) must
        map every None case to the same generic error response."""
        token_hash = hash_reset_token(raw_token)
        return await password_reset_repository.consume_token_and_apply_reset(
            self._pool, token_hash, new_password_hash,
        )
