"""V1 account recovery pass: POST /password/forgot and
POST /password/reset. Covers account-enumeration safety (response body
AND timing), hashed single-use tokens, expiry, concurrent double-use,
session-family revocation, Redis-outage safety, abuse rate limiting,
and the durable out-of-band email-delivery boundary (independent-review
timing-enumeration fix).

No test in this file ever sends, or could send, a real email --
delivery jobs enqueued here are inspected directly at the database
layer (decrypted with the real app.security.reset_delivery_crypto
module and the test environment's own configured key), never processed
by a running worker, since /password/forgot itself must never depend
on that happening.
"""
import asyncio
import json

import pytest
from redis.exceptions import RedisError

from app.config import settings
from app.security.reset_delivery_crypto import decrypt_delivery_payload
from app.security.tokens import hash_refresh_token


@pytest.fixture(autouse=True)
def fast_forgot_password_timing(monkeypatch):
    """This file is about /password/forgot's OTHER behaviors
    (enumeration safety, token lifecycle, rate limiting, the delivery
    boundary) -- app.main._FORGOT_PASSWORD_TIMING_NORMALIZER's own
    real jitter/sleep is exercised deliberately, deterministically, and
    without any real sleep in tests/domain/test_timing_normalization.py
    instead. Patching sleep_fn to a no-op here just keeps this file
    fast; it does not change which branch ran or what target duration
    would have been chosen."""
    import app.main as main_module

    async def _no_sleep(seconds):
        return None

    monkeypatch.setattr(main_module._FORGOT_PASSWORD_TIMING_NORMALIZER, "sleep_fn", _no_sleep)


async def _signup(client, email="resettest@test.com", password="testpass123"):
    resp = await client.post("/signup", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()


async def _count_enqueued_delivery_jobs(db_pool) -> int:
    return await db_pool.fetchval("SELECT count(*) FROM jobs WHERE job_type = 'password_reset_email'")


async def _decrypt_latest_delivery_job(db_pool) -> dict:
    """Reads the most recently enqueued password_reset_email job
    directly from the durable `jobs` table and decrypts its payload
    with the real crypto module -- this is exactly, and only, what
    app/workers/password_reset_email_worker.py itself would do, just
    without a worker process actually running during these HTTP-level
    tests (proving the request path never depends on one)."""
    row = await db_pool.fetchrow(
        "SELECT payload FROM jobs WHERE job_type = 'password_reset_email' ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None, "no password_reset_email job was enqueued"
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return decrypt_delivery_payload(payload["encrypted_delivery"], settings.password_reset_email_delivery_key)


async def _get_raw_token_from_latest_job(db_pool) -> str:
    delivery = await _decrypt_latest_delivery_job(db_pool)
    return delivery["reset_url"].split("token=")[1]


# ---------------------------------------------------------------------------
# POST /password/forgot -- account enumeration safety (response body)
# ---------------------------------------------------------------------------


async def test_forgot_password_existing_account_returns_generic_message(client, db_pool):
    await _signup(client, email="existing@test.com")
    resp = await client.post("/password/forgot", json={"email": "existing@test.com"})
    assert resp.status_code == 200
    assert "message" in resp.json()
    assert await _count_enqueued_delivery_jobs(db_pool) == 1
    delivery = await _decrypt_latest_delivery_job(db_pool)
    assert delivery["to_email"] == "existing@test.com"


async def test_forgot_password_nonexistent_email_returns_identical_response(client, db_pool):
    existing = await client.post("/password/forgot", json={"email": "doesnotexist@test.com"})
    await _signup(client, email="realaccount@test.com")
    real = await client.post("/password/forgot", json={"email": "realaccount@test.com"})

    assert existing.status_code == real.status_code == 200
    assert existing.json() == real.json()
    # Only the real account actually got a delivery job queued.
    assert await _count_enqueued_delivery_jobs(db_pool) == 1


async def test_forgot_password_disabled_account_indistinguishable(client, db_pool):
    await _signup(client, email="disabledforgot@test.com")
    await db_pool.execute("UPDATE users SET is_active = false WHERE email = $1", "disabledforgot@test.com")

    resp = await client.post("/password/forgot", json={"email": "disabledforgot@test.com"})
    baseline = await client.post("/password/forgot", json={"email": "nonexistent-baseline@test.com"})

    assert resp.status_code == baseline.status_code == 200
    assert resp.json() == baseline.json()
    assert await _count_enqueued_delivery_jobs(db_pool) == 0  # never queued for a disabled account


async def test_forgot_password_deleted_account_indistinguishable(client, db_pool):
    await _signup(client, email="deletedforgot@test.com")
    await db_pool.execute("UPDATE users SET deleted_at = now() WHERE email = $1", "deletedforgot@test.com")

    resp = await client.post("/password/forgot", json={"email": "deletedforgot@test.com"})
    baseline = await client.post("/password/forgot", json={"email": "nonexistent-baseline-2@test.com"})

    assert resp.status_code == baseline.status_code == 200
    assert resp.json() == baseline.json()
    assert await _count_enqueued_delivery_jobs(db_pool) == 0


async def test_forgot_password_malformed_email_is_a_normal_validation_error(client):
    """Not an enumeration signal -- 422 for a syntactically invalid
    email happens before any lookup, identically regardless of what
    (if anything) exists at that non-address."""
    resp = await client.post("/password/forgot", json={"email": "not-an-email"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Durable email-delivery boundary (independent-review timing-enumeration fix)
# ---------------------------------------------------------------------------


async def test_forgot_password_never_calls_provider_http_directly(client, db_pool, monkeypatch):
    """The single most important structural proof this fix requires:
    POST /password/forgot must not perform outbound provider network
    I/O in its own request path. Guards httpx.AsyncClient.post (the
    one place ResendTransactionalEmailService -- and any future
    provider adapter built the same way -- would issue that call) so
    ONLY a call to Resend's own endpoint raises; the test client's own
    calls (also httpx.AsyncClient.post, against the ASGI transport)
    pass through to the real implementation unaffected, since it is
    literally the same class/method both use."""
    import httpx

    original_post = httpx.AsyncClient.post

    async def _fail_only_for_resend(self, url, *args, **kwargs):
        if "api.resend.com" in str(url):
            raise AssertionError("POST /password/forgot must never perform outbound HTTP itself")
        return await original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fail_only_for_resend)

    await _signup(client, email="nonetworkcall@test.com")
    resp = await client.post("/password/forgot", json={"email": "nonetworkcall@test.com"})
    assert resp.status_code == 200
    assert await _count_enqueued_delivery_jobs(db_pool) == 1


async def test_forgot_password_eligible_request_returns_before_delivery_is_processed(client, db_pool):
    """The delivery job must still be 'pending' (unclaimed) immediately
    after the HTTP response comes back -- proving the request path
    genuinely hands delivery off rather than processing it inline
    under a different name."""
    await _signup(client, email="returnsbeforedelivery@test.com")
    resp = await client.post("/password/forgot", json={"email": "returnsbeforedelivery@test.com"})
    assert resp.status_code == 200

    row = await db_pool.fetchrow(
        "SELECT status FROM jobs WHERE job_type = 'password_reset_email' ORDER BY created_at DESC LIMIT 1"
    )
    assert row["status"] == "pending"


async def test_forgot_password_response_unaffected_by_provider_failure(client, db_pool, monkeypatch):
    """Provider failure must never be exposed to the POST /password/forgot
    caller -- trivially guaranteed by construction now (the route never
    awaits the provider at all), proven dynamically here by breaking
    only calls to Resend's own endpoint and confirming the response is
    still the normal 200 generic body. See the previous test's own
    docstring for why only Resend's URL is guarded, not every
    httpx.AsyncClient.post call (the test client itself uses the same
    method against the ASGI transport)."""
    import httpx

    original_post = httpx.AsyncClient.post

    async def _fail_only_for_resend(self, url, *args, **kwargs):
        if "api.resend.com" in str(url):
            raise httpx.ConnectError("simulated total provider outage")
        return await original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fail_only_for_resend)

    await _signup(client, email="providerdown@test.com")
    resp = await client.post("/password/forgot", json={"email": "providerdown@test.com"})
    assert resp.status_code == 200
    assert resp.json() == {
        "message": "If an eligible account exists for that email, password reset instructions will be sent."
    }


async def test_plaintext_reset_token_never_appears_in_the_jobs_table(client, db_pool):
    await _signup(client, email="jobsplaintextcheck@test.com")
    await client.post("/password/forgot", json={"email": "jobsplaintextcheck@test.com"})

    raw_token = await _get_raw_token_from_latest_job(db_pool)

    raw_payload_text = await db_pool.fetchval(
        "SELECT payload::text FROM jobs WHERE job_type = 'password_reset_email' ORDER BY created_at DESC LIMIT 1"
    )
    assert raw_token not in raw_payload_text
    assert "jobsplaintextcheck@test.com" not in raw_payload_text


# ---------------------------------------------------------------------------
# Token storage: hashed only, plaintext never persisted
# ---------------------------------------------------------------------------


async def test_reset_token_stored_hashed_never_plaintext(client, db_pool):
    await _signup(client, email="hashcheck@test.com")
    await client.post("/password/forgot", json={"email": "hashcheck@test.com"})

    raw_token = await _get_raw_token_from_latest_job(db_pool)

    row = await db_pool.fetchrow(
        "SELECT token_hash FROM password_reset_tokens WHERE user_id = (SELECT id FROM users WHERE email = $1)",
        "hashcheck@test.com",
    )
    assert row is not None
    assert row["token_hash"] != raw_token
    assert len(row["token_hash"]) == 64  # sha256 hex digest length
    assert row["token_hash"] == hash_refresh_token(raw_token)  # same sha256-hex shape as refresh tokens

    # And the raw token genuinely never appears anywhere in the table.
    all_hashes = await db_pool.fetch("SELECT token_hash FROM password_reset_tokens")
    for r in all_hashes:
        assert raw_token not in r["token_hash"]


async def test_issuing_new_token_invalidates_previous_outstanding_token(client, db_pool):
    await _signup(client, email="reissue@test.com")
    await client.post("/password/forgot", json={"email": "reissue@test.com"})
    first_raw = await _get_raw_token_from_latest_job(db_pool)

    await client.post("/password/forgot", json={"email": "reissue@test.com"})
    second_raw = await _get_raw_token_from_latest_job(db_pool)

    assert first_raw != second_raw

    # The first (now-superseded) token no longer resets the password.
    resp = await client.post("/password/reset", json={"token": first_raw, "new_password": "newpassword123"})
    assert resp.status_code == 400

    # The second (current) token still works.
    resp2 = await client.post("/password/reset", json={"token": second_raw, "new_password": "newpassword123"})
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# POST /password/reset -- expiry, single-use, malformed/invalid tokens
# ---------------------------------------------------------------------------


async def test_reset_with_expired_token_rejected(client, db_pool):
    signup = await _signup(client, email="expiredreset@test.com")
    user_id = signup["id"]
    raw_token = "expired-token-raw-value-for-test-only"
    token_hash = hash_refresh_token(raw_token)
    await db_pool.execute(
        "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, now() - interval '1 minute')",
        user_id, token_hash,
    )

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert resp.status_code == 400
    assert "detail" in resp.json()


async def test_reset_token_single_use(client, db_pool):
    await _signup(client, email="singleuse@test.com")
    await client.post("/password/forgot", json={"email": "singleuse@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)

    first = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert first.status_code == 200

    second = await client.post("/password/reset", json={"token": raw_token, "new_password": "anotherpassword456"})
    assert second.status_code == 400


async def test_reset_concurrent_double_use_exactly_one_succeeds(client, db_pool):
    """Part 5: two simultaneous attempts with the same token must
    result in exactly one success -- proven against the real database
    under real concurrency, not mocked."""
    await _signup(client, email="concurrentreset@test.com")
    await client.post("/password/forgot", json={"email": "concurrentreset@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)

    results = await asyncio.gather(
        client.post("/password/reset", json={"token": raw_token, "new_password": "passwordone11"}),
        client.post("/password/reset", json={"token": raw_token, "new_password": "passwordtwo22"}),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 400]


async def test_reset_with_malformed_token_rejected_generically(client):
    resp = await client.post("/password/reset", json={"token": "not-a-real-token-at-all", "new_password": "newpassword123"})
    assert resp.status_code == 400


async def test_reset_with_unknown_but_well_formed_token_rejected_generically(client):
    import secrets

    resp = await client.post(
        "/password/reset", json={"token": secrets.token_urlsafe(32), "new_password": "newpassword123"},
    )
    assert resp.status_code == 400


async def test_reset_password_policy_enforced(client, db_pool):
    """No separate password policy -- the same min_length=8 rule
    /signup already enforces via Pydantic Field."""
    await _signup(client, email="policytest@test.com")
    await client.post("/password/forgot", json={"email": "policytest@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "short"})
    assert resp.status_code == 422


async def test_reset_account_no_longer_eligible_rejected(client, db_pool):
    """A token issued while the account was still active must not
    reset the password once the account has since been disabled --
    the atomic UPDATE's password_reset_account_eligible() check covers
    this even though the token itself is otherwise still
    valid/unused/unexpired."""
    await _signup(client, email="disabledbeforereset@test.com")
    await client.post("/password/forgot", json={"email": "disabledbeforereset@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)

    await db_pool.execute("UPDATE users SET is_active = false WHERE email = $1", "disabledbeforereset@test.com")

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Session revocation after a successful reset
# ---------------------------------------------------------------------------


async def test_successful_reset_does_not_auto_authenticate(client, db_pool):
    await _signup(client, email="noautologin@test.com")
    await client.post("/password/forgot", json={"email": "noautologin@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" not in body
    assert "refresh_token" not in body


async def test_all_refresh_families_revoked_after_reset(client, db_pool):
    await _signup(client, email="revokeall@test.com", password="oldpassword123")
    login1 = await client.post("/login", json={"email": "revokeall@test.com", "password": "oldpassword123"})
    login2 = await client.post("/login", json={"email": "revokeall@test.com", "password": "oldpassword123"})
    assert login1.status_code == login2.status_code == 200

    old_refresh_1 = login1.json()["refresh_token"]
    old_refresh_2 = login2.json()["refresh_token"]
    old_access_1 = login1.json()["access_token"]
    old_access_2 = login2.json()["access_token"]

    await client.post("/password/forgot", json={"email": "revokeall@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)
    reset_resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "brandnewpassword123"})
    assert reset_resp.status_code == 200

    # Both pre-existing refresh tokens (different families) are rejected.
    r1 = await client.post("/refresh", json={"refresh_token": old_refresh_1})
    r2 = await client.post("/refresh", json={"refresh_token": old_refresh_2})
    assert r1.status_code == 401
    assert r2.status_code == 401

    # Both pre-existing access tokens are rejected too (System
    # Integrity Gate V1's Postgres-authoritative family check).
    me1 = await client.get("/me", headers={"Authorization": f"Bearer {old_access_1}"})
    me2 = await client.get("/me", headers={"Authorization": f"Bearer {old_access_2}"})
    assert me1.status_code == 401
    assert me2.status_code == 401

    # The new password actually works -- sign-in with the new password
    # is required, exactly as the reset was supposed to enable.
    relogin = await client.post("/login", json={"email": "revokeall@test.com", "password": "brandnewpassword123"})
    assert relogin.status_code == 200

    # The old password no longer works.
    old_login = await client.post("/login", json={"email": "revokeall@test.com", "password": "oldpassword123"})
    assert old_login.status_code == 401


async def test_redis_outage_after_reset_does_not_restore_old_sessions(client, db_pool, monkeypatch):
    """System Integrity Gate V1: Postgres is the durable revocation
    authority, Redis only a best-effort cache. Simulating a Redis
    outage for the post-reset cache-propagation step must not leave
    the old access token usable -- get_current_user's Postgres check
    (refresh_tokens.revoked_at) is what actually rejects it, same as
    /logout-all's own Redis-outage safety."""
    await _signup(client, email="redisoutage@test.com", password="oldpassword123")
    login = await client.post("/login", json={"email": "redisoutage@test.com", "password": "oldpassword123"})
    old_access = login.json()["access_token"]

    await client.post("/password/forgot", json={"email": "redisoutage@test.com"})
    raw_token = await _get_raw_token_from_latest_job(db_pool)

    import app.main as main_module

    class _BrokenRedis:
        async def set(self, *args, **kwargs):
            raise RedisError("simulated Redis outage")

    monkeypatch.setattr(main_module, "get_redis", lambda: _BrokenRedis())

    reset_resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "brandnewpassword123"})
    assert reset_resp.status_code == 200  # the durable reset itself must not fail merely because Redis is down

    me_after = await client.get("/me", headers={"Authorization": f"Bearer {old_access}"})
    assert me_after.status_code == 401


# ---------------------------------------------------------------------------
# Abuse protection
# ---------------------------------------------------------------------------


async def test_forgot_password_rate_limited_per_email(client):
    """PASSWORD_RESET_EMAIL_POLICY is a module-level constant computed
    once from settings at import time (app/middleware/rate_limiter.py)
    -- monkeypatching settings.rate_limit_password_reset_email_max
    after import would not affect it, so this exercises the real
    configured default (5) directly rather than trying to override it."""
    from app.middleware.rate_limiter import PASSWORD_RESET_EMAIL_POLICY

    for _ in range(PASSWORD_RESET_EMAIL_POLICY.max_requests):
        resp = await client.post("/password/forgot", json={"email": "ratelimited@test.com"})
        assert resp.status_code == 200

    denied = await client.post("/password/forgot", json={"email": "ratelimited@test.com"})
    assert denied.status_code == 429
    assert "Retry-After" in denied.headers


async def test_forgot_password_rate_limit_is_per_email_not_global(client):
    """A different target address is unaffected by another address
    having been rate-limited -- the limiter must not become a global
    kill switch for the endpoint."""
    from app.middleware.rate_limiter import PASSWORD_RESET_EMAIL_POLICY

    for _ in range(PASSWORD_RESET_EMAIL_POLICY.max_requests):
        resp = await client.post("/password/forgot", json={"email": "ratelimited-a@test.com"})
        assert resp.status_code == 200
    blocked = await client.post("/password/forgot", json={"email": "ratelimited-a@test.com"})
    assert blocked.status_code == 429

    other = await client.post("/password/forgot", json={"email": "ratelimited-b@test.com"})
    assert other.status_code == 200
