"""System Integrity Gate V1, section 4: Postgres is the durable auth
revocation authority. Redis remains a fast-deny cache layer only --
these tests prove get_current_user's authoritative check is genuinely
backed by Postgres (the same durable refresh_tokens rows /logout,
/logout-all, refresh-replay-detection, and account deletion already
write), not by whatever happens to still be in Redis, by making Redis
itself unreachable and proving revocation still works (and ordinary
valid sessions still authenticate) anyway.
"""
import uuid

import pytest
from redis.exceptions import RedisError

from app.security.tokens import create_access_token


class _AlwaysFailingRedisProxy:
    """Every operation immediately raises RedisError -- used where the
    intent is "Redis is completely unreachable" and the route(s) under
    test sit behind GENERAL_POLICY (fail_open=True, see
    app/middleware/rate_limiter.py), so no real network connection
    attempt (with redis-py's own retry/backoff behavior, which a
    genuinely closed port can turn into several real seconds per call)
    is needed -- this fails every call deterministically and
    instantly instead."""

    def __getattr__(self, name):
        async def _raise(*args, **kwargs):
            raise RedisError(f"simulated Redis unavailability ({name})")
        return _raise


class _SetFailingRedisProxy:
    """Wraps a real, working Redis client but makes `set()` fail --
    isolates "the revocation-marker cache-propagation write
    specifically fails" from "Redis is completely unreachable".

    The distinction matters for /logout and /refresh specifically:
    both sit behind `rate_limit_by_ip(AUTH_POLICY)`
    (app/middleware/rate_limiter.py), whose `fail_open=False` is a
    separate, deliberate, pre-existing security posture (reject with
    503 rather than let an unverifiable request through an abuse-prone
    auth endpoint) that this pass does not touch. That rate-limit
    check runs via `RateLimiter.check()`'s `redis.eval(...)` call,
    BEFORE the route body -- swapping out the whole client (as
    `_AlwaysFailingRedisProxy` does, for the routes that can safely
    take that) would trip that unrelated fail-closed check first and
    503 before this pass's own logout/refresh logic ever ran, testing
    the wrong thing entirely. This proxy leaves
    `eval` (and everything else) working against the real Redis so the
    rate limiter is unaffected, while only the `set()` call this
    module's own revocation-marker propagation uses actually fails."""

    def __init__(self, real_client):
        self._real = real_client

    async def set(self, *args, **kwargs):
        raise RedisError("simulated Redis failure propagating a revocation marker")

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _signup_and_login(client, email="revocation-authority@test.com", password="testpass123"):
    await client.post("/signup", json={"email": email, "password": password})
    resp = await client.post("/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()


async def test_logout_db_succeeds_redis_fails_old_access_token_still_rejected(client, redis_client, monkeypatch):
    tokens = await _signup_and_login(client, email="logout-redisdown@test.com")
    access_token, refresh_token = tokens["access_token"], tokens["refresh_token"]

    import app.redis_client as redis_module
    monkeypatch.setattr(redis_module, "_redis_client", _SetFailingRedisProxy(redis_client))

    logout_resp = await client.post("/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 200  # Redis failure must not surface as a logout failure

    me_after = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_after.status_code == 401  # rejected by Postgres alone, Redis still down


async def test_logout_all_db_succeeds_redis_fails_all_old_access_tokens_rejected(client, monkeypatch):
    tokens_a = await _signup_and_login(client, email="logoutall-redisdown@test.com")
    access_a = tokens_a["access_token"]

    # A second login for the same user -- a second family/session.
    login_b = await client.post(
        "/login", json={"email": "logoutall-redisdown@test.com", "password": "testpass123"},
    )
    access_b = login_b.json()["access_token"]

    import app.redis_client as redis_module
    monkeypatch.setattr(redis_module, "_redis_client", _AlwaysFailingRedisProxy())

    logout_all_resp = await client.post("/logout-all", headers={"Authorization": f"Bearer {access_a}"})
    assert logout_all_resp.status_code == 200

    me_a = await client.get("/me", headers={"Authorization": f"Bearer {access_a}"})
    assert me_a.status_code == 401
    me_b = await client.get("/me", headers={"Authorization": f"Bearer {access_b}"})
    assert me_b.status_code == 401


async def test_refresh_replay_revokes_family_while_redis_unavailable(client, redis_client, monkeypatch):
    tokens = await _signup_and_login(client, email="replay-redisdown@test.com")
    original_refresh, original_access = tokens["refresh_token"], tokens["access_token"]

    first = await client.post("/refresh", json={"refresh_token": original_refresh})
    assert first.status_code == 200

    import app.redis_client as redis_module
    monkeypatch.setattr(redis_module, "_redis_client", _SetFailingRedisProxy(redis_client))

    replay = await client.post("/refresh", json={"refresh_token": original_refresh})
    assert replay.status_code == 401  # the replay request itself still returns 401

    me_original = await client.get("/me", headers={"Authorization": f"Bearer {original_access}"})
    assert me_original.status_code == 401  # family-wide revocation committed to Postgres despite Redis being down


async def test_account_delete_db_succeeds_redis_fails_access_token_rejected(client, monkeypatch):
    tokens = await _signup_and_login(client, email="delete-redisdown@test.com")
    access_token = tokens["access_token"]

    import app.redis_client as redis_module
    monkeypatch.setattr(redis_module, "_redis_client", _AlwaysFailingRedisProxy())

    delete_resp = await client.delete("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert delete_resp.status_code == 200

    me_after = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_after.status_code == 401


async def test_redis_unavailable_during_ordinary_valid_authentication_still_authenticates(client, monkeypatch):
    """The other half of section 4's required posture: Redis being
    unreachable must never fail-closed a session Postgres can still
    validate directly. Before this pass, get_current_user returned 503
    the instant Redis raised, even for a perfectly valid, unrevoked
    session."""
    tokens = await _signup_and_login(client, email="validauth-redisdown@test.com")
    access_token = tokens["access_token"]

    import app.redis_client as redis_module
    monkeypatch.setattr(redis_module, "_redis_client", _AlwaysFailingRedisProxy())

    me = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me.status_code == 200


async def test_family_id_belonging_to_another_user_is_rejected(client, db_pool):
    """A forged/mismatched access token -- correctly signed (this
    server minted it), but claiming a family_id that actually belongs
    to a different user's refresh_tokens rows -- must be rejected by
    the authoritative Postgres check, which explicitly requires the
    family to belong to *this* token's own user_id, not just to exist
    for someone."""
    await _signup_and_login(client, email="family-owner-a@test.com")
    tokens_b = await _signup_and_login(client, email="family-owner-b@test.com")

    user_a_id = await db_pool.fetchval("SELECT id FROM users WHERE email = $1", "family-owner-a@test.com")
    family_id_b = await db_pool.fetchval(
        "SELECT family_id FROM refresh_tokens WHERE user_id = (SELECT id FROM users WHERE email = $1)",
        "family-owner-b@test.com",
    )

    forged = create_access_token(str(user_a_id), str(family_id_b))
    resp = await client.get("/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_unknown_fabricated_family_id_is_rejected(client, db_pool):
    await _signup_and_login(client, email="fabricated-family@test.com")
    user_id = await db_pool.fetchval("SELECT id FROM users WHERE email = $1", "fabricated-family@test.com")

    forged = create_access_token(str(user_id), str(uuid.uuid4()))
    resp = await client.get("/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
