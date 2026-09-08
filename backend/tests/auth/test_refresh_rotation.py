"""Phase 2: refresh token rotation correctness.

Covers: concurrent refresh (exactly one winner), replay detection
(whole family revoked), successor-insert-failure rollback, expired
token, revoked token, and logout family revocation.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.security.tokens import generate_refresh_token


async def _signup_and_login(client, email="rotationtest@test.com", password="testpass123"):
    await client.post("/signup", json={"email": email, "password": password})
    resp = await client.post("/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()


async def test_concurrent_refresh_exactly_one_succeeds(client):
    tokens = await _signup_and_login(client)
    refresh_token = tokens["refresh_token"]

    results = await asyncio.gather(
        client.post("/refresh", json={"refresh_token": refresh_token}),
        client.post("/refresh", json={"refresh_token": refresh_token}),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 401]


async def test_replay_revokes_entire_family(client):
    tokens = await _signup_and_login(client, email="replaytest@test.com")
    original_refresh = tokens["refresh_token"]
    original_access = tokens["access_token"]

    first = await client.post("/refresh", json={"refresh_token": original_refresh})
    assert first.status_code == 200
    new_access = first.json()["access_token"]

    # Replay the already-consumed original token.
    replay = await client.post("/refresh", json={"refresh_token": original_refresh})
    assert replay.status_code == 401

    # The successor access token minted by the legitimate refresh must
    # now be rejected too -- the whole family died, not just the replay.
    me = await client.get("/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 401
    assert me.json()["detail"] == "Token has been revoked"

    # The pre-replay original access token is also part of the same
    # family and must be rejected.
    me_original = await client.get("/me", headers={"Authorization": f"Bearer {original_access}"})
    assert me_original.status_code == 401


async def test_successor_insert_failure_rolls_back(client, db_pool, monkeypatch):
    tokens = await _signup_and_login(client, email="rollbacktest@test.com")
    original_refresh = tokens["refresh_token"]
    original_hash_row = await db_pool.fetchrow(
        "SELECT token_hash, used_at FROM refresh_tokens WHERE user_id = (SELECT id FROM users WHERE email = $1)",
        "rollbacktest@test.com",
    )
    assert original_hash_row["used_at"] is None

    # Force the successor's token_hash to collide with an existing row,
    # so the INSERT inside the transaction genuinely fails with a real
    # UniqueViolationError -- not a mock standing in for one.
    colliding_raw, colliding_hash = generate_refresh_token()
    await db_pool.execute(
        """
        INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)
        VALUES ((SELECT id FROM users WHERE email = $1), gen_random_uuid(), $2, now() + interval '1 day')
        """,
        "rollbacktest@test.com",
        colliding_hash,
    )

    import app.main as main_module
    monkeypatch.setattr(main_module, "generate_refresh_token", lambda: (colliding_raw, colliding_hash))

    resp = await client.post("/refresh", json={"refresh_token": original_refresh})
    assert resp.status_code == 500  # unhandled UniqueViolationError -- a genuine failure, not swallowed

    # The critical assertion: the original token's consumption was
    # rolled back along with the failed insert. State remains coherent.
    row_after = await db_pool.fetchrow(
        "SELECT used_at FROM refresh_tokens WHERE token_hash = $1",
        original_hash_row["token_hash"],
    )
    assert row_after["used_at"] is None

    # Restore the real generate_refresh_token before retrying -- otherwise
    # the retry would hit the exact same collision and this would prove
    # nothing about recovery, only that the patch was still active.
    monkeypatch.undo()

    # And because it's unconsumed, a real retry with the original token
    # now succeeds.
    retry = await client.post("/refresh", json={"refresh_token": original_refresh})
    assert retry.status_code == 200


async def test_expired_token_rejected(client, db_pool):
    tokens = await _signup_and_login(client, email="expiredtest@test.com")
    user_id = await db_pool.fetchval("SELECT id FROM users WHERE email = $1", "expiredtest@test.com")

    raw, token_hash = generate_refresh_token()
    await db_pool.execute(
        """
        INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)
        VALUES ($1, gen_random_uuid(), $2, now() - interval '1 hour')
        """,
        user_id, token_hash,
    )

    resp = await client.post("/refresh", json={"refresh_token": raw})
    assert resp.status_code == 401


async def test_revoked_token_rejected(client, db_pool):
    tokens = await _signup_and_login(client, email="revokedtest@test.com")
    user_id = await db_pool.fetchval("SELECT id FROM users WHERE email = $1", "revokedtest@test.com")

    raw, token_hash = generate_refresh_token()
    await db_pool.execute(
        """
        INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at, revoked_at)
        VALUES ($1, gen_random_uuid(), $2, now() + interval '1 day', now())
        """,
        user_id, token_hash,
    )

    resp = await client.post("/refresh", json={"refresh_token": raw})
    assert resp.status_code == 401


async def test_logout_revokes_family_and_blocks_refresh(client):
    tokens = await _signup_and_login(client, email="logouttest@test.com")
    refresh_token = tokens["refresh_token"]

    logout_resp = await client.post("/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 200

    refresh_resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 401
