"""V1 account-recovery orchestration: forgot-password issuance and
reset-password completion (see ACCOUNT_RECOVERY_ARCHITECTURE.md). The
only module that ties together password_reset_repository (Postgres),
TransactionalEmailService (the provider-neutral email boundary, Part
3), and reset-token generation -- POST /password/forgot and
POST /password/reset in app/main.py call this, not the repository or
email service directly, so the enumeration-safety and atomicity
invariants documented here live in exactly one place.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

import asyncpg

from app.db import password_reset_repository
from app.domain.transactional_email import TransactionalEmailService

logger = logging.getLogger(__name__)


def generate_reset_token() -> Tuple[str, str]:
    """Same shape as app.security.tokens.generate_refresh_token(): a
    cryptographically secure random raw token (secrets.token_urlsafe,
    not random/uuid4) plus its SHA-256 hex digest. Only the digest is
    ever persisted; the raw value exists only transiently -- in this
    process's memory while building the reset URL, and in the outbound
    email body -- and must never be logged, returned in any API
    response, or passed to app.observability.events."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_reset_token(raw)


def hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PasswordResetService:
    def __init__(
        self, pool: asyncpg.Pool, email_service: TransactionalEmailService,
        *, url_base: str, token_ttl_minutes: int,
    ):
        self._pool = pool
        self._email_service = email_service
        self._url_base = url_base
        self._token_ttl_minutes = token_ttl_minutes

    async def request_reset(self, email: str) -> None:
        """Always returns normally -- never raises, and never signals
        anything different, for a nonexistent email, a disabled
        account (is_active=false), or a deleted account (deleted_at
        set). POST /password/forgot's outward response is identical
        regardless of what this method actually did, which is exactly
        what makes account enumeration impossible through this
        endpoint."""
        eligible = await password_reset_repository.lookup_eligible_user_by_email(self._pool, email)
        if eligible is None or not eligible["is_active"] or eligible["deleted_at"] is not None:
            return

        raw_token, token_hash = generate_reset_token()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self._token_ttl_minutes)
        await password_reset_repository.issue_reset_token(self._pool, eligible["id"], token_hash, expires_at)

        reset_url = f"{self._url_base}?token={raw_token}"
        try:
            await self._email_service.send_password_reset(to_email=email, reset_url=reset_url)
        except Exception:
            # The reset token is already durably issued above
            # regardless of whether delivery succeeds -- a
            # transactional-email provider outage must not turn a
            # legitimate forgot-password request into a 500
            # distinguishable from every other outcome's identical
            # generic response. Deliberately no email address, reset
            # URL, or token in this log line.
            logger.warning("password_reset: transactional email delivery failed")

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
