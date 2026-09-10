"""Atomic quota-reservation ledger (migration ee276e90a60f), independent
of any billing provider -- see app/domain/entitlement.py for the
policy layer built on top of this.

Concurrency guarantee (Phase 9's actual requirement -- "allowance 3,
10 simultaneous requests -> exactly 3 succeed, not 4, not 7"):
reserve() takes a Postgres advisory transaction lock
(pg_advisory_xact_lock) keyed by (user_id, period_key) *before* doing
anything else -- every concurrent reservation attempt for the same
user+period is fully serialized through that one line, so the
idempotency check and the quota-count-then-insert that follow it are
never racing another concurrent attempt for the same user+period. The
lock is released automatically at transaction end (commit or
rollback); no manual unlock, and nothing is held across requests.

Idempotency guarantee: request_id is globally UNIQUE (schema-level).
reserve() checks for an existing row by request_id *inside* the locked
section (not before acquiring the lock -- see the docstring on why
that ordering matters) and returns it, replay=True, for any status
including RELEASED -- matching the same idempotency philosophy already
established by app/queue/postgres_queue.py's jobs table: one
request_id maps to one row forever, and a genuinely new attempt
requires a new request_id, not a retry of an old one.

analysis_usage has row-level security (migration ee276e90a60f), same
pattern as user_profiles/consent_events: every query here sets
app.current_user_id as the first statement of its transaction.
"""
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg

RESERVED = "RESERVED"
CONSUMED = "CONSUMED"
RELEASED = "RELEASED"
# In-memory-only outcome, never a value the `status` column itself can
# hold (see the table's CHECK constraint) -- returned when the
# advisory-locked count-check finds the user's allowance for this
# period already exhausted, so no row is inserted at all.
DENIED = "DENIED"


@dataclass(frozen=True)
class Reservation:
    id: Optional[UUID]
    status: str  # RESERVED | CONSUMED | RELEASED | DENIED
    replay: bool  # True if this is an existing row returned via idempotent replay, not a fresh reservation


async def reserve(
    pool: asyncpg.Pool,
    user_id: UUID,
    request_id: str,
    period_key: str,
    allowance: int,
) -> Reservation:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))

            # Must be acquired before the idempotency check below, not
            # just before the count check -- otherwise two concurrent
            # callers with the *same* request_id could both pass the
            # pre-lock existing-row check (seeing no row yet), then
            # serialize through the lock one after another, and the
            # second would attempt a second INSERT with the same
            # request_id after the first already committed one.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{user_id}:{period_key}",
            )

            existing = await conn.fetchrow(
                "SELECT id, status FROM analysis_usage WHERE request_id = $1", request_id
            )
            if existing is not None:
                return Reservation(id=existing["id"], status=existing["status"], replay=True)

            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM analysis_usage
                WHERE user_id = $1 AND period_key = $2 AND status IN ('RESERVED', 'CONSUMED')
                """,
                user_id, period_key,
            )
            if count >= allowance:
                return Reservation(id=None, status=DENIED, replay=False)

            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO analysis_usage (user_id, request_id, period_key, status, reserved_at)
                    VALUES ($1, $2, $3, 'RESERVED', now())
                    RETURNING id, status
                    """,
                    user_id, request_id, period_key,
                )
            except asyncpg.exceptions.UniqueViolationError:
                # Defensive fallback for the one case the advisory
                # lock above doesn't cover: the same request_id reused
                # under a *different* period_key (e.g. a retry landing
                # just after a period boundary) racing a concurrent
                # insert under the original period_key -- different
                # lock keys, so not serialized against each other, but
                # the schema's own UNIQUE(request_id) still catches it.
                existing = await conn.fetchrow(
                    "SELECT id, status FROM analysis_usage WHERE request_id = $1", request_id
                )
                return Reservation(id=existing["id"], status=existing["status"], replay=True)

            return Reservation(id=row["id"], status=row["status"], replay=False)


async def consume(pool: asyncpg.Pool, user_id: UUID, reservation_id: UUID) -> None:
    """Marks a RESERVED reservation CONSUMED after the analysis it
    gated actually succeeded. A no-op (not an error) if the
    reservation is no longer RESERVED -- consume/release are both
    terminal-state transitions a retried caller might legitimately
    attempt twice."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_usage SET status = 'CONSUMED', consumed_at = now() "
                "WHERE id = $1 AND status = 'RESERVED'",
                reservation_id,
            )


async def release(pool: asyncpg.Pool, user_id: UUID, reservation_id: UUID) -> None:
    """Marks a RESERVED reservation RELEASED after the analysis it
    gated failed for a legitimate reason (not the user's fault) --
    frees the quota slot back up: release()d reservations are excluded
    from reserve()'s COUNT(*) check, so a released slot can be
    consumed again by a *new* request_id (not this same one -- see
    this module's docstring on why request_id maps to one row
    forever)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_usage SET status = 'RELEASED', released_at = now() "
                "WHERE id = $1 AND status = 'RESERVED'",
                reservation_id,
            )
