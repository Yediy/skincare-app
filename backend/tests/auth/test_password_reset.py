"""V1 account recovery pass: POST /password/forgot and
POST /password/reset. Covers account-enumeration safety, hashed
single-use tokens, expiry, concurrent double-use, session-family
revocation, Redis-outage safety, and abuse rate limiting.

Every test that needs an email actually delivered wires a real
app.domain.transactional_email.InMemoryTransactionalEmailService in
place of build_transactional_email_service() -- no test in this file
ever sends, or could send, a real email.
"""
import asyncio

import pytest
from redis.exceptions import RedisError

from app.domain.transactional_email import InMemoryTransactionalEmailService
from app.security.tokens import hash_refresh_token


@pytest.fixture(autouse=True)
def fake_email_service(monkeypatch):
    """Every test in this file gets a fresh InMemoryTransactionalEmailService
    wired in place of the real factory. app.main's routes do
    `from app.domain.transactional_email import build_transactional_email_service`
    fresh inside each request handler, so patching the name where it's
    actually defined (not a copy already bound into app.main's module
    namespace, which doesn't exist until that import statement runs)
    is what takes effect."""
    import app.domain.transactional_email as email_module

    fake = InMemoryTransactionalEmailService()
    monkeypatch.setattr(email_module, "build_transactional_email_service", lambda: fake)
    return fake


async def _signup(client, email="resettest@test.com", password="testpass123"):
    resp = await client.post("/signup", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# POST /password/forgot -- account enumeration safety
# ---------------------------------------------------------------------------


async def test_forgot_password_existing_account_returns_generic_message(client, fake_email_service):
    await _signup(client, email="existing@test.com")
    resp = await client.post("/password/forgot", json={"email": "existing@test.com"})
    assert resp.status_code == 200
    assert "message" in resp.json()
    assert len(fake_email_service.sent) == 1
    assert fake_email_service.sent[0]["to_email"] == "existing@test.com"


async def test_forgot_password_nonexistent_email_returns_identical_response(client, fake_email_service):
    existing = await client.post("/password/forgot", json={"email": "doesnotexist@test.com"})
    await _signup(client, email="realaccount@test.com")
    real = await client.post("/password/forgot", json={"email": "realaccount@test.com"})

    assert existing.status_code == real.status_code == 200
    assert existing.json() == real.json()
    # Only the real account actually got an email queued.
    assert len(fake_email_service.sent) == 1
    assert fake_email_service.sent[0]["to_email"] == "realaccount@test.com"


async def test_forgot_password_disabled_account_indistinguishable(client, db_pool, fake_email_service):
    await _signup(client, email="disabledforgot@test.com")
    await db_pool.execute("UPDATE users SET is_active = false WHERE email = $1", "disabledforgot@test.com")

    resp = await client.post("/password/forgot", json={"email": "disabledforgot@test.com"})
    baseline = await client.post("/password/forgot", json={"email": "nonexistent-baseline@test.com"})

    assert resp.status_code == baseline.status_code == 200
    assert resp.json() == baseline.json()
    assert fake_email_service.sent == []  # never emailed a disabled account


async def test_forgot_password_deleted_account_indistinguishable(client, db_pool, fake_email_service):
    await _signup(client, email="deletedforgot@test.com")
    await db_pool.execute("UPDATE users SET deleted_at = now() WHERE email = $1", "deletedforgot@test.com")

    resp = await client.post("/password/forgot", json={"email": "deletedforgot@test.com"})
    baseline = await client.post("/password/forgot", json={"email": "nonexistent-baseline-2@test.com"})

    assert resp.status_code == baseline.status_code == 200
    assert resp.json() == baseline.json()
    assert fake_email_service.sent == []


async def test_forgot_password_malformed_email_is_a_normal_validation_error(client):
    """Not an enumeration signal -- 422 for a syntactically invalid
    email happens before any lookup, identically regardless of what
    (if anything) exists at that non-address."""
    resp = await client.post("/password/forgot", json={"email": "not-an-email"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Token storage: hashed only, plaintext never persisted
# ---------------------------------------------------------------------------


async def test_reset_token_stored_hashed_never_plaintext(client, db_pool, fake_email_service):
    await _signup(client, email="hashcheck@test.com")
    await client.post("/password/forgot", json={"email": "hashcheck@test.com"})

    raw_token = fake_email_service.sent[0]["reset_url"].split("token=")[1]

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


async def test_issuing_new_token_invalidates_previous_outstanding_token(client, db_pool, fake_email_service):
    await _signup(client, email="reissue@test.com")
    await client.post("/password/forgot", json={"email": "reissue@test.com"})
    first_raw = fake_email_service.sent[0]["reset_url"].split("token=")[1]

    await client.post("/password/forgot", json={"email": "reissue@test.com"})
    second_raw = fake_email_service.sent[1]["reset_url"].split("token=")[1]

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


async def test_reset_with_expired_token_rejected(client, db_pool, fake_email_service):
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


async def test_reset_token_single_use(client, fake_email_service):
    await _signup(client, email="singleuse@test.com")
    await client.post("/password/forgot", json={"email": "singleuse@test.com"})
    raw_token = fake_email_service.sent[0]["reset_url"].split("token=")[1]

    first = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert first.status_code == 200

    second = await client.post("/password/reset", json={"token": raw_token, "new_password": "anotherpassword456"})
    assert second.status_code == 400


async def test_reset_concurrent_double_use_exactly_one_succeeds(client, fake_email_service):
    """Part 5: two simultaneous attempts with the same token must
    result in exactly one success -- proven against the real database
    under real concurrency, not mocked."""
    await _signup(client, email="concurrentreset@test.com")
    await client.post("/password/forgot", json={"email": "concurrentreset@test.com"})
    raw_token = fake_email_service.sent[0]["reset_url"].split("token=")[1]

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


async def test_reset_password_policy_enforced(client, fake_email_service):
    """No separate password policy -- the same min_length=8 rule
    /signup already enforces via Pydantic Field."""
    await _signup(client, email="policytest@test.com")
    await client.post("/password/forgot", json={"email": "policytest@test.com"})
    raw_token = fake_email_service.sent[0]["reset_url"].split("token=")[1]

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "short"})
    assert resp.status_code == 422


async def test_reset_account_no_longer_eligible_rejected(client, db_pool, fake_email_service):
    """A token issued while the account was still active must not
    reset the password once the account has since been disabled --
    the atomic UPDATE's password_reset_account_eligible() check covers
    this even though the token itself is otherwise still
    valid/unused/unexpired."""
    await _signup(client, email="disabledbeforereset@test.com")
    await client.post("/password/forgot", json={"email": "disabledbeforereset@test.com"})
    raw_token = fake_email_service.sent[0]["reset_url"].split("token=")[1]

    await db_pool.execute("UPDATE users SET is_active = false WHERE email = $1", "disabledbeforereset@test.com")

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Session revocation after a successful reset
# ---------------------------------------------------------------------------


async def test_successful_reset_does_not_auto_authenticate(client, fake_email_service):
    await _signup(client, email="noautologin@test.com")
    await client.post("/password/forgot", json={"email": "noautologin@test.com"})
    raw_token = fake_email_service.sent[0]["reset_url"].split("token=")[1]

    resp = await client.post("/password/reset", json={"token": raw_token, "new_password": "newpassword123"})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" not in body
    assert "refresh_token" not in body


async def test_all_refresh_families_revoked_after_reset(client, fake_email_service):
    await _signup(client, email="revokeall@test.com", password="oldpassword123")
    login1 = await client.post("/login", json={"email": "revokeall@test.com", "password": "oldpassword123"})
    login2 = await client.post("/login", json={"email": "revokeall@test.com", "password": "oldpassword123"})
    assert login1.status_code == login2.status_code == 200

    old_refresh_1 = login1.json()["refresh_token"]
    old_refresh_2 = login2.json()["refresh_token"]
    old_access_1 = login1.json()["access_token"]
    old_access_2 = login2.json()["access_token"]

    await client.post("/password/forgot", json={"email": "revokeall@test.com"})
    raw_token = fake_email_service.sent[-1]["reset_url"].split("token=")[1]
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


async def test_redis_outage_after_reset_does_not_restore_old_sessions(client, fake_email_service, monkeypatch):
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
    raw_token = fake_email_service.sent[-1]["reset_url"].split("token=")[1]

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


async def test_forgot_password_rate_limited_per_email(client, fake_email_service):
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


async def test_forgot_password_rate_limit_is_per_email_not_global(client, fake_email_service):
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
