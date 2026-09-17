"""Migration 44a74f2a79a7's BEFORE UPDATE trigger
(enforce_analysis_request_status_transition): analysis_requests.status
must follow RECEIVED -> QUEUED -> PROCESSING -> COMPLETED, with
FAILED/CANCELLED reachable from any non-terminal state and
PROCESSING -> PROCESSING allowed for a legitimate re-entrant claim --
enforced by the database itself, not just repository-layer discipline.
Every illegal transition below is attempted with a raw UPDATE against
the superuser pool, deliberately bypassing app/db/analysis_repository.py
entirely, so these tests prove the database-level guarantee exists
independent of any Python code ever calling it correctly.
"""
import uuid

import asyncpg
import pytest


async def _create_user(db_pool, email: str):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def _create_request(db_pool, user_id, status: str):
    row = await db_pool.fetchrow(
        """
        INSERT INTO analysis_requests (user_id, request_id, status)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        user_id, str(uuid.uuid4()), status,
    )
    return row["id"]


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("COMPLETED", "PROCESSING"),
        ("COMPLETED", "FAILED"),
        ("FAILED", "PROCESSING"),
        ("FAILED", "COMPLETED"),
        ("CANCELLED", "PROCESSING"),
        ("CANCELLED", "COMPLETED"),
    ],
)
async def test_terminal_status_can_never_transition_out(db_pool, from_status, to_status):
    user_id = await _create_user(db_pool, f"transition-{from_status}-{to_status}@test.com".lower())
    request_id = await _create_request(db_pool, user_id, from_status)

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db_pool.execute("UPDATE analysis_requests SET status = $2 WHERE id = $1", request_id, to_status)

    row = await db_pool.fetchrow("SELECT status FROM analysis_requests WHERE id = $1", request_id)
    assert row["status"] == from_status  # the rejected UPDATE changed nothing


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("RECEIVED", "PROCESSING"),
        ("RECEIVED", "COMPLETED"),
        ("QUEUED", "COMPLETED"),
        ("PROCESSING", "QUEUED"),
        ("PROCESSING", "RECEIVED"),
    ],
)
async def test_other_invalid_transitions_are_also_rejected(db_pool, from_status, to_status):
    user_id = await _create_user(db_pool, f"transition-other-{from_status}-{to_status}@test.com".lower())
    request_id = await _create_request(db_pool, user_id, from_status)

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db_pool.execute("UPDATE analysis_requests SET status = $2 WHERE id = $1", request_id, to_status)


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("RECEIVED", "QUEUED"),
        ("RECEIVED", "FAILED"),
        ("RECEIVED", "CANCELLED"),
        ("QUEUED", "PROCESSING"),
        ("QUEUED", "FAILED"),
        ("QUEUED", "CANCELLED"),
        ("PROCESSING", "COMPLETED"),
        ("PROCESSING", "FAILED"),
        ("PROCESSING", "CANCELLED"),
    ],
)
async def test_documented_valid_transitions_are_allowed(db_pool, from_status, to_status):
    user_id = await _create_user(db_pool, f"transition-valid-{from_status}-{to_status}@test.com".lower())
    request_id = await _create_request(db_pool, user_id, from_status)

    await db_pool.execute("UPDATE analysis_requests SET status = $2 WHERE id = $1", request_id, to_status)

    row = await db_pool.fetchrow("SELECT status FROM analysis_requests WHERE id = $1", request_id)
    assert row["status"] == to_status


async def test_processing_to_processing_reentrant_claim_is_allowed(db_pool):
    """The one same-status transition that must succeed unconditionally
    -- a legitimate worker re-entering PROCESSING on a reclaimed job.
    The trigger's WHEN clause never even evaluates it (status doesn't
    change), so this is really proving the trigger doesn't accidentally
    block it, not proving new trigger logic."""
    user_id = await _create_user(db_pool, "transition-processing-reentrant@test.com")
    request_id = await _create_request(db_pool, user_id, "PROCESSING")

    await db_pool.execute(
        "UPDATE analysis_requests SET status = 'PROCESSING', started_at = now() WHERE id = $1", request_id,
    )

    row = await db_pool.fetchrow("SELECT status FROM analysis_requests WHERE id = $1", request_id)
    assert row["status"] == "PROCESSING"


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "CANCELLED"])
async def test_terminal_same_status_write_is_an_idempotent_no_op(db_pool, terminal_status):
    """Same-state terminal writes are allowed (never blocked outright)
    but must never alter terminal meaning -- a same-status UPDATE never
    even reaches the trigger's transition-graph check at all."""
    user_id = await _create_user(db_pool, f"transition-idempotent-{terminal_status}@test.com".lower())
    request_id = await _create_request(db_pool, user_id, terminal_status)

    await db_pool.execute(
        "UPDATE analysis_requests SET status = $2 WHERE id = $1", request_id, terminal_status,
    )

    row = await db_pool.fetchrow("SELECT status FROM analysis_requests WHERE id = $1", request_id)
    assert row["status"] == terminal_status
