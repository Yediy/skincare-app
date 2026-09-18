"""Consumes `password_reset_email` jobs enqueued by
app/domain/password_reset_service.py::PasswordResetService.request_reset,
performing the actual outbound TransactionalEmailService call this
codebase's independent review required be removed from POST
/password/forgot's own request path (a timing-based account-
enumeration side channel -- see app/domain/timing_normalization.py).
Entry point:

    python -m app.workers.password_reset_email_worker

Same claim -> process -> acknowledge/fail(retryable) shape as
app/workers/revenuecat_webhook_worker.py, deliberately without a
heartbeat loop: sending one email is a handful of fast operations, well
inside the default claim visibility window.

Security-critical: this is the ONLY module that ever decrypts a
password-reset delivery payload (app/security/reset_delivery_crypto.py)
back into a plaintext raw reset token / reset URL, and it does so only
in process memory, immediately before handing it to
TransactionalEmailService -- never logged, never persisted anywhere
else. Once a job reaches a terminal state (delivered, or permanently
failed), its payload is redacted in place (`_redact_payload`) so the
encrypted ciphertext does not linger in the `jobs` table indefinitely
after it's no longer needed.
"""
import asyncio
import logging
from datetime import datetime, timezone

from app.domain.transactional_email import TransactionalEmailError, TransactionalEmailService
from app.observability import events as observability_events
from app.queue.base import Job, JobLeaseLostError, JobQueue
from app.security.reset_delivery_crypto import ResetDeliveryDecryptionError, decrypt_delivery_payload

logger = logging.getLogger(__name__)

PASSWORD_RESET_EMAIL_JOB_TYPE = "password_reset_email"
# Stable per-job provider idempotency identity (see
# TransactionalEmailService.send_password_reset's own docstring) --
# `f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}"`, never a
# value generated per attempt.
PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX = "password-reset"
CLAIM_VISIBILITY_TIMEOUT_SECONDS = 60
EMPTY_QUEUE_POLL_INTERVAL_SECONDS = 2.0

_REDACTED_PAYLOAD = {"redacted": True}


async def _redact_payload(pool, job_id) -> None:
    """Overwrites a terminal (delivered or permanently failed) job's
    payload so the encrypted delivery ciphertext -- and the token_hash
    alongside it -- do not remain in `jobs` any longer than the job
    itself is still meaningfully pending. Best-effort: a failure here
    never affects the ack/fail outcome that already committed."""
    try:
        await pool.execute("UPDATE jobs SET payload = $2::jsonb WHERE id = $1", job_id, '{"redacted": true}')
    except Exception:
        logger.warning("password_reset_email_worker: job=%s payload redaction failed", job_id)


async def _process_job(
    job: Job, job_queue: JobQueue, pool, email_service: TransactionalEmailService, delivery_encryption_key: str,
) -> None:
    from app.db import password_reset_repository

    token_hash = job.payload.get("token_hash")

    try:
        # Respect token expiry / avoid sending obviously stale links:
        # a newer /password/forgot call may have invalidated this exact
        # token (issue_reset_token invalidates a user's prior
        # outstanding tokens), or it may simply have expired while this
        # job sat in the queue. Either way, `used_at` is no longer NULL
        # or `expires_at` has passed, and there is nothing useful left
        # to send -- acknowledge as a no-op rather than emailing a link
        # that can never work.
        status = await password_reset_repository.get_token_status(pool, token_hash) if token_hash else None
        now = datetime.now(timezone.utc)
        if status is None or status["used_at"] is not None or status["expires_at"] <= now:
            await job_queue.acknowledge(job.id, job.claim_token)
            await _redact_payload(pool, job.id)
            logger.info("password_reset_email_worker: job=%s skipped -- token no longer sendable", job.id)
            return

        delivery = decrypt_delivery_payload(job.payload["encrypted_delivery"], delivery_encryption_key)
        # The durable job's own id is already the stable identity every
        # reclaim/retry of this exact delivery shares -- deriving the
        # provider idempotency key from it (rather than generating a
        # fresh one per attempt) is what lets Resend itself recognize a
        # retry after a crash/lease-loss that happened between "Resend
        # accepted this" and this process's own acknowledge(), and
        # refuse to dispatch a second email for it.
        idempotency_key = f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}"
        await email_service.send_password_reset(
            to_email=delivery["to_email"], reset_url=delivery["reset_url"], idempotency_key=idempotency_key,
        )

        await job_queue.acknowledge(job.id, job.claim_token)
        await _redact_payload(pool, job.id)
        logger.info("password_reset_email_worker: job=%s delivered", job.id)

    except JobLeaseLostError:
        # System Integrity Gate V1: this job's lease already expired
        # and was reclaimed by a newer worker -- STALE ATTEMPT /
        # ABANDON, same as every other worker in this codebase. No
        # further compensation; the newer worker owns this job's
        # outcome (and will itself decide whether to send, skip, or
        # retry).
        logger.warning("password_reset_email_worker: job=%s abandoned -- lease lost", job.id)

    except (ResetDeliveryDecryptionError, KeyError):
        # A malformed/corrupt/wrong-key-encrypted payload cannot become
        # sendable on retry -- terminal, not retryable. Deliberately no
        # payload contents in this log line.
        try:
            await job_queue.fail(job.id, job.claim_token, "UNDECRYPTABLE_PAYLOAD", retryable=False)
        except JobLeaseLostError:
            logger.warning("password_reset_email_worker: job=%s abandoned -- lease lost while reporting failure", job.id)
            return
        await _redact_payload(pool, job.id)
        logger.error("password_reset_email_worker: job=%s failed -- undecryptable payload", job.id)

    except TransactionalEmailError as e:
        # Provider/network failure -- retryable, same backoff/
        # max-attempts policy every other job type in this queue
        # already uses (PostgresJobQueue.fail). Never exposed to the
        # POST /password/forgot caller, which returned long before this
        # runs. Deliberately no recipient/reset-URL/token in this log
        # line -- only the exception class name, matching
        # app/domain/password_reset_service.py's own logging discipline.
        try:
            is_terminal = await job_queue.fail(job.id, job.claim_token, e.__class__.__name__, retryable=True)
        except JobLeaseLostError:
            logger.warning("password_reset_email_worker: job=%s abandoned -- lease lost while reporting failure", job.id)
            return
        logger.warning("password_reset_email_worker: job=%s delivery failed terminal=%s", job.id, is_terminal)
        if is_terminal:
            await _redact_payload(pool, job.id)
            observability_events.password_reset_email_delivery_exhausted()


async def run_one_cycle(
    job_queue: JobQueue, pool, email_service: TransactionalEmailService, delivery_encryption_key: str,
) -> bool:
    job = await job_queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE, visibility_timeout_seconds=CLAIM_VISIBILITY_TIMEOUT_SECONDS)
    if job is None:
        return False
    await _process_job(job, job_queue, pool, email_service, delivery_encryption_key)
    return True


async def run_forever(
    job_queue: JobQueue, pool, email_service: TransactionalEmailService, delivery_encryption_key: str,
    *, poll_interval_seconds: float = EMPTY_QUEUE_POLL_INTERVAL_SECONDS,
) -> None:
    while True:
        claimed = await run_one_cycle(job_queue, pool, email_service, delivery_encryption_key)
        if not claimed:
            await asyncio.sleep(poll_interval_seconds)


async def _main() -> None:
    from app.config import settings
    from app.db.connection import close_db_pool, get_db_pool, init_db_pool
    from app.domain.transactional_email import build_transactional_email_service
    from app.queue.postgres_queue import PostgresJobQueue

    await init_db_pool()
    try:
        pool = get_db_pool()
        job_queue = PostgresJobQueue(pool)
        email_service = build_transactional_email_service()
        logger.info("password_reset_email_worker: starting main loop")
        await run_forever(job_queue, pool, email_service, settings.password_reset_email_delivery_key)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
