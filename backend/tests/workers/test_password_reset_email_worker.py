"""app.workers.password_reset_email_worker -- the durable, out-of-band
consumer of `password_reset_email` jobs (independent-review timing-
enumeration fix: POST /password/forgot's own request path no longer
performs any outbound provider network I/O; this worker is the only
place that still does, and only ever after the HTTP response has
already been returned).

Run through app_db_pool (the restricted skincare_app role), same as
tests/queue/test_postgres_job_queue.py -- real Postgres, not mocked.
Every email "delivery" in this file goes through
InMemoryTransactionalEmailService, never a real network call.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from app.config import settings
from app.db import password_reset_repository
from app.domain.password_reset_service import PASSWORD_RESET_EMAIL_JOB_TYPE, generate_reset_token
from app.domain.transactional_email import (
    InMemoryTransactionalEmailService,
    ResendTransactionalEmailService,
    TransactionalEmailError,
)
from app.queue.postgres_queue import PostgresJobQueue
from app.security.reset_delivery_crypto import encrypt_delivery_payload
from app.workers.password_reset_email_worker import (
    PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX,
    _process_job,
)

DELIVERY_KEY = settings.password_reset_email_delivery_key


@pytest.fixture
def queue(app_db_pool):
    return PostgresJobQueue(app_db_pool)


async def _make_user(db_pool, email="workertest@test.com"):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email,
    )
    return row["id"]


async def _issue_token_and_enqueue(
    app_db_pool, queue, user_id: UUID, email: str, *, expires_in_minutes: int = 30,
):
    raw_token, token_hash = generate_reset_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
    await password_reset_repository.issue_reset_token(app_db_pool, user_id, token_hash, expires_at)
    reset_url = f"https://app.test.invalid/reset?token={raw_token}"
    encrypted = encrypt_delivery_payload({"to_email": email, "reset_url": reset_url}, DELIVERY_KEY)
    job = await queue.enqueue(PASSWORD_RESET_EMAIL_JOB_TYPE, {"token_hash": token_hash, "encrypted_delivery": encrypted})
    return job, raw_token, token_hash, reset_url


async def test_process_job_delivers_email_and_acknowledges_and_redacts_payload(db_pool, app_db_pool, queue):
    user_id = await _make_user(db_pool, "delivered@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "delivered@test.com")

    claimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    fake_email = InMemoryTransactionalEmailService()
    await _process_job(claimed, queue, app_db_pool, fake_email, DELIVERY_KEY)

    assert fake_email.sent == [{
        "to_email": "delivered@test.com",
        "reset_url": reset_url,
        "idempotency_key": f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}",
    }]

    row = await db_pool.fetchrow("SELECT status, payload FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"
    import json
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload == {"redacted": True}
    assert raw_token not in json.dumps(payload)


async def test_process_job_skips_send_for_a_superseded_used_token(db_pool, app_db_pool, queue):
    """A newer /password/forgot call invalidates a user's prior
    outstanding tokens (issue_reset_token) -- if that happens before
    this queued job is processed, the worker must not send a now-
    pointless (and misleading) reset link."""
    user_id = await _make_user(db_pool, "superseded@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "superseded@test.com")

    # Simulate a second forgot-password request superseding this token.
    await db_pool.execute("UPDATE password_reset_tokens SET used_at = now() WHERE token_hash = $1", token_hash)

    claimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    fake_email = InMemoryTransactionalEmailService()
    await _process_job(claimed, queue, app_db_pool, fake_email, DELIVERY_KEY)

    assert fake_email.sent == []
    row = await db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"


async def test_process_job_skips_send_for_an_expired_token(db_pool, app_db_pool, queue):
    user_id = await _make_user(db_pool, "expiredworker@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(
        app_db_pool, queue, user_id, "expiredworker@test.com", expires_in_minutes=-1,
    )

    claimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    fake_email = InMemoryTransactionalEmailService()
    await _process_job(claimed, queue, app_db_pool, fake_email, DELIVERY_KEY)

    assert fake_email.sent == []
    row = await db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"


async def test_process_job_retries_on_provider_failure_without_redacting(db_pool, app_db_pool, queue):
    user_id = await _make_user(db_pool, "retrytest@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "retrytest@test.com")

    claimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)

    class _AlwaysFailsEmailService:
        async def send_password_reset(self, *, to_email, reset_url, idempotency_key):
            raise TransactionalEmailError("simulated provider outage")

    await _process_job(claimed, queue, app_db_pool, _AlwaysFailsEmailService(), DELIVERY_KEY)

    row = await db_pool.fetchrow("SELECT status, attempt_count, payload FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "pending"  # requeued for retry, not terminal
    assert row["attempt_count"] == 1
    # Payload must NOT be redacted yet -- the next retry attempt still
    # needs it to actually send.
    import json
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload != {"redacted": True}
    assert "encrypted_delivery" in payload


async def test_process_job_redacts_payload_once_retries_are_exhausted(db_pool, app_db_pool, queue):
    user_id = await _make_user(db_pool, "exhaustedtest@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "exhaustedtest@test.com")

    # Fast-forward attempt_count to one below max_attempts (default 3)
    # so this single failure is the terminal one -- avoids three real
    # claim/fail cycles just to reach the same outcome.
    await db_pool.execute("UPDATE jobs SET attempt_count = 2 WHERE id = $1", job.id)

    claimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)

    class _AlwaysFailsEmailService:
        async def send_password_reset(self, *, to_email, reset_url, idempotency_key):
            raise TransactionalEmailError("simulated provider outage")

    await _process_job(claimed, queue, app_db_pool, _AlwaysFailsEmailService(), DELIVERY_KEY)

    row = await db_pool.fetchrow("SELECT status, payload FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "failed"
    import json
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload == {"redacted": True}


async def test_delivery_survives_a_simulated_worker_crash_before_acknowledge(db_pool, app_db_pool, queue):
    """Durability proof: a worker that claims a job and then crashes
    (never acknowledges, never fails it) must not silently lose the
    reset email -- once its claim's visibility window lapses, a
    restarted/second worker can claim and successfully process the
    exact same job. visibility_timeout_seconds=0 simulates "already
    expired" without a real sleep, same technique
    tests/queue/test_postgres_job_queue.py's own reclaim tests use."""
    user_id = await _make_user(db_pool, "crashsim@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "crashsim@test.com")

    crashed_worker_claim = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE, visibility_timeout_seconds=0)
    assert crashed_worker_claim is not None
    # The crashed worker never calls acknowledge()/fail() from here.

    restarted_worker_claim = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    assert restarted_worker_claim is not None
    assert restarted_worker_claim.id == job.id
    assert restarted_worker_claim.claim_token != crashed_worker_claim.claim_token

    fake_email = InMemoryTransactionalEmailService()
    await _process_job(restarted_worker_claim, queue, app_db_pool, fake_email, DELIVERY_KEY)

    assert fake_email.sent == [{
        "to_email": "crashsim@test.com",
        "reset_url": reset_url,
        "idempotency_key": f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}",
    }]
    row = await db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"


async def test_jobs_payload_never_contains_the_plaintext_token(db_pool, app_db_pool, queue):
    """Persistence-layer proof complementing
    tests/security/test_reset_delivery_crypto.py's crypto-level one:
    the raw token is never present anywhere in the durable `jobs` row,
    from the moment it's enqueued, before any worker ever touches it."""
    user_id = await _make_user(db_pool, "plaintextcheck@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(
        app_db_pool, queue, user_id, "plaintextcheck@test.com",
    )

    raw_row = await db_pool.fetchval("SELECT payload::text FROM jobs WHERE id = $1", job.id)
    assert raw_token not in raw_row
    assert "plaintextcheck@test.com" not in raw_row


async def test_idempotency_key_is_stable_and_contains_no_recipient_or_token(db_pool, app_db_pool, queue):
    """Requirement: the SAME durable job must produce the SAME provider
    idempotency key on every attempt, and that key must never carry the
    recipient email or the raw reset token -- it is a delivery
    identity, not a copy of the sensitive payload it deduplicates."""
    user_id = await _make_user(db_pool, "idempotencyformat@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(
        app_db_pool, queue, user_id, "idempotencyformat@test.com",
    )

    captured_keys = []

    class _CapturingEmailService:
        async def send_password_reset(self, *, to_email, reset_url, idempotency_key):
            captured_keys.append(idempotency_key)

    claimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    await _process_job(claimed, queue, app_db_pool, _CapturingEmailService(), DELIVERY_KEY)

    assert captured_keys == [f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}"]
    key = captured_keys[0]
    assert str(job.id) in key
    assert "idempotencyformat@test.com" not in key
    assert "@" not in key
    assert raw_token not in key


async def test_different_jobs_get_different_idempotency_keys(db_pool, app_db_pool, queue):
    user_a = await _make_user(db_pool, "differentjobsa@test.com")
    user_b = await _make_user(db_pool, "differentjobsb@test.com")
    job_a, *_ = await _issue_token_and_enqueue(app_db_pool, queue, user_a, "differentjobsa@test.com")
    job_b, *_ = await _issue_token_and_enqueue(app_db_pool, queue, user_b, "differentjobsb@test.com")
    assert job_a.id != job_b.id

    captured_keys = []

    class _CapturingEmailService:
        async def send_password_reset(self, *, to_email, reset_url, idempotency_key):
            captured_keys.append(idempotency_key)

    claimed_a = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    await _process_job(claimed_a, queue, app_db_pool, _CapturingEmailService(), DELIVERY_KEY)
    claimed_b = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    await _process_job(claimed_b, queue, app_db_pool, _CapturingEmailService(), DELIVERY_KEY)

    assert len(captured_keys) == 2
    assert captured_keys[0] != captured_keys[1]


async def test_idempotency_key_survives_retry_of_the_same_job(db_pool, app_db_pool, queue):
    """Same durable job, two separate processing attempts (a provider
    failure followed by a retry) -- both attempts must present the
    identical idempotency key, since it is derived only from job.id."""
    user_id = await _make_user(db_pool, "retrysamekey@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "retrysamekey@test.com")

    captured_keys = []

    class _FailsOnceEmailService:
        def __init__(self):
            self.calls = 0

        async def send_password_reset(self, *, to_email, reset_url, idempotency_key):
            captured_keys.append(idempotency_key)
            self.calls += 1
            if self.calls == 1:
                raise TransactionalEmailError("simulated transient provider outage")

    email_service = _FailsOnceEmailService()

    first_attempt = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    await _process_job(first_attempt, queue, app_db_pool, email_service, DELIVERY_KEY)

    # The retryable failure above scheduled next_attempt_at with a real
    # backoff delay (see PostgresJobQueue.fail) -- not yet claimable.
    # Same technique tests/queue/test_postgres_job_queue.py's own retry
    # tests use to skip past it without a real sleep.
    await app_db_pool.execute("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = $1", job.id)

    second_attempt = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    assert second_attempt is not None
    assert second_attempt.id == job.id
    await _process_job(second_attempt, queue, app_db_pool, email_service, DELIVERY_KEY)

    assert captured_keys == [
        f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}",
        f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}",
    ]


async def test_crash_after_provider_success_and_reclaim_cannot_create_two_provider_deliveries(
    db_pool, app_db_pool, queue,
):
    """Duplicate-delivery fence: simulates the exact scenario the
    independent review flagged -- Resend accepts the email, then the
    worker crashes/loses its lease before acknowledge(), the job
    becomes claimable again, and a second worker retries it. Because
    both attempts derive their Idempotency-Key from the SAME job.id
    (never a per-attempt random value) and pass it through
    ResendTransactionalEmailService as the real `Idempotency-Key`
    header, a real Resend-side idempotency check (simulated here by the
    MockTransport handler counting distinct keys, exactly as
    https://resend.com/docs/api-reference/emails/send-email#idempotency-key
    documents) collapses the two POSTs into one logical delivery."""
    user_id = await _make_user(db_pool, "crashduplicate@test.com")
    job, raw_token, token_hash, reset_url = await _issue_token_and_enqueue(app_db_pool, queue, user_id, "crashduplicate@test.com")

    deliveries_by_key: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        # A real Idempotency-Key-aware provider returns the ORIGINAL
        # response for a repeated key rather than performing a second
        # send -- deliveries_by_key models that: only the first POST
        # for a given key counts as a new delivery.
        is_new_delivery = key not in deliveries_by_key
        deliveries_by_key.setdefault(key, 0)
        deliveries_by_key[key] += 1
        return httpx.Response(200, json={"id": "email-id-123", "new_delivery": is_new_delivery})

    resend = ResendTransactionalEmailService(
        api_key="test-resend-key", from_email="noreply@test.invalid", transport=httpx.MockTransport(handler),
    )

    # "Crashed" worker: claims with an already-expired visibility window
    # (same technique test_delivery_survives_a_simulated_worker_crash_
    # before_acknowledge uses) so the claim itself is immediately
    # reclaimable, then calls the provider directly with the same key
    # the real worker would derive -- modeling "Resend accepted this,
    # then the process died before acknowledge() ran".
    crashed_claim = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE, visibility_timeout_seconds=0)
    assert crashed_claim is not None
    crashed_key = f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{crashed_claim.id}"
    await resend.send_password_reset(to_email="crashduplicate@test.com", reset_url=reset_url, idempotency_key=crashed_key)
    # No acknowledge()/fail() -- this worker crashed right here.

    # Reclaim: a second worker claims the exact same job and runs the
    # real worker code path end to end.
    reclaimed = await queue.claim(PASSWORD_RESET_EMAIL_JOB_TYPE)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    await _process_job(reclaimed, queue, app_db_pool, resend, DELIVERY_KEY)

    assert crashed_key == f"{PASSWORD_RESET_EMAIL_IDEMPOTENCY_KEY_PREFIX}/{job.id}"
    # Two POSTs reached the transport, both under the identical key --
    # a real Resend-side idempotency check would only ever have
    # dispatched one email.
    assert deliveries_by_key == {crashed_key: 2}

    row = await db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"
