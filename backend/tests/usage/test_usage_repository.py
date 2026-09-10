"""Real PostgreSQL tests for app.db.usage_repository (Phases 7/9 of
the product/usage foundation pass). Run through app_db_pool -- the
restricted skincare_app role, same as production -- not the
superuser, not mocked. See that module's own docstring for the
concurrency/idempotency guarantees being proven here.
"""
import asyncio
import uuid

import pytest

from app.db import usage_repository


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def test_reserve_creates_a_reserved_row(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "usage-reserve@test.com")
    result = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=3)
    assert result.status == usage_repository.RESERVED
    assert result.replay is False
    assert result.id is not None


async def test_reserve_beyond_allowance_is_denied(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "usage-deny@test.com")
    for _ in range(3):
        result = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=3)
        assert result.status == usage_repository.RESERVED

    denied = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=3)
    assert denied.status == usage_repository.DENIED
    assert denied.id is None


async def test_reserve_with_same_request_id_replays_not_duplicates(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "usage-replay@test.com")
    request_id = str(uuid.uuid4())

    first = await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=3)
    second = await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=3)

    assert first.id == second.id
    assert first.replay is False
    assert second.replay is True


async def test_replay_does_not_count_twice_against_allowance(db_pool, app_db_pool):
    """A retried request_id must not consume a second slot -- reserving
    with allowance=1 twice under the same request_id must both
    succeed (the second as a replay), and a genuinely new request_id
    must then be denied."""
    user_id = await _create_user(db_pool, "usage-replay-quota@test.com")
    request_id = str(uuid.uuid4())

    first = await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=1)
    second = await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=1)
    assert first.status == second.status == usage_repository.RESERVED

    third = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=1)
    assert third.status == usage_repository.DENIED


async def test_consume_marks_reservation_consumed(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "usage-consume@test.com")
    reservation = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=3)

    await usage_repository.consume(app_db_pool, user_id, reservation.id)

    row = await db_pool.fetchrow("SELECT status, consumed_at FROM analysis_usage WHERE id = $1", reservation.id)
    assert row["status"] == "CONSUMED"
    assert row["consumed_at"] is not None


async def test_release_marks_reservation_released_and_frees_a_slot(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "usage-release@test.com")
    reservation = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=1)

    await usage_repository.release(app_db_pool, user_id, reservation.id)

    row = await db_pool.fetchrow("SELECT status, released_at FROM analysis_usage WHERE id = $1", reservation.id)
    assert row["status"] == "RELEASED"
    assert row["released_at"] is not None

    # allowance was 1 and the only reservation was released -- a *new*
    # request_id must be able to reserve again.
    fresh = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=1)
    assert fresh.status == usage_repository.RESERVED


async def test_released_reservations_request_id_is_not_reusable_for_a_fresh_reservation(db_pool, app_db_pool):
    """Matches the same idempotency philosophy as the jobs table
    (app/queue/postgres_queue.py): a request_id maps to one row
    forever, including after release -- a retry with that same
    request_id returns the RELEASED row itself, not a new reservation.
    A genuinely new attempt requires a new request_id."""
    user_id = await _create_user(db_pool, "usage-release-replay@test.com")
    request_id = str(uuid.uuid4())
    reservation = await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=3)
    await usage_repository.release(app_db_pool, user_id, reservation.id)

    replay = await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=3)
    assert replay.id == reservation.id
    assert replay.status == usage_repository.RELEASED
    assert replay.replay is True


async def test_concurrent_reservations_never_exceed_allowance(db_pool, app_db_pool):
    """The core quota guarantee: allowance=3, 10 simultaneous unique
    requests -> exactly 3 reservations succeed. Not 4. Not 7. Proven
    with a real concurrent race (asyncio.gather), not asserted from
    reading the SQL."""
    user_id = await _create_user(db_pool, "usage-concurrent@test.com")
    allowance = 3
    request_ids = [str(uuid.uuid4()) for _ in range(10)]

    results = await asyncio.gather(*(
        usage_repository.reserve(app_db_pool, user_id, rid, "2026-09", allowance) for rid in request_ids
    ))

    reserved = [r for r in results if r.status == usage_repository.RESERVED]
    denied = [r for r in results if r.status == usage_repository.DENIED]

    assert len(reserved) == 3
    assert len(denied) == 7
    assert len({r.id for r in reserved}) == 3  # three genuinely distinct reservations


async def test_concurrent_identical_request_id_produces_exactly_one_reservation(db_pool, app_db_pool):
    """10 concurrent requests with the SAME request_id -> one logical
    reservation, not ten, and not a UniqueViolationError bubbling up
    to any caller."""
    user_id = await _create_user(db_pool, "usage-concurrent-idempotent@test.com")
    request_id = str(uuid.uuid4())

    results = await asyncio.gather(*(
        usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance=5) for _ in range(10)
    ))

    ids = {r.id for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if not r.replay) == 1  # exactly one call actually created it
    assert sum(1 for r in results if r.replay) == 9  # the rest are replays

    row_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_usage WHERE user_id = $1 AND request_id = $2", user_id, request_id
    )
    assert row_count == 1


async def test_different_users_have_independent_allowances(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "usage-independent-a@test.com")
    user_b = await _create_user(db_pool, "usage-independent-b@test.com")

    for _ in range(2):
        result = await usage_repository.reserve(app_db_pool, user_a, str(uuid.uuid4()), "2026-09", allowance=2)
        assert result.status == usage_repository.RESERVED

    # user_a is now at their limit -- user_b, same period, is unaffected.
    result_b = await usage_repository.reserve(app_db_pool, user_b, str(uuid.uuid4()), "2026-09", allowance=2)
    assert result_b.status == usage_repository.RESERVED
