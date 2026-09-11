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

Idempotency + retry semantics (Part II, Phase 14 -- corrected this
pass): request_id is globally UNIQUE (schema-level) and maps to
exactly one row, but that row's status is no longer permanently
terminal the way the previous pass's docstring claimed. Three distinct
outcomes for an existing row, matched exactly to this pass's own
specification:

  - CONSUMED: the logical operation already completed. Never reserve
    again, never compute again -- reserve() returns it as a replay
    with status=CONSUMED and takes no other action; the caller
    (app/domain/entitlement.py's UsagePolicyService) is the layer that
    turns that into "don't recompute."
  - RESERVED: another attempt for this exact request_id is already
    pending/in-flight (a concurrent caller, or a still-running earlier
    attempt that hasn't reached consume()/release() yet). reserve()
    returns it as a replay with status=RESERVED and
    just_reactivated=False -- the caller must not create duplicate
    compute for it.
  - RELEASED: the previous attempt failed for a legitimate reason and
    gave its slot back. This one may be re-reserved -- atomically,
    subject to *current* quota availability (a released slot does not
    grant a free pass around the allowance check), transitioning
    RELEASED -> RESERVED and incrementing attempt_count. Returned as a
    replay with status=RESERVED and just_reactivated=True, so the
    caller can tell "you may proceed, this is a legitimate retry"
    apart from the plain-RESERVED "someone else already owns this"
    case above -- both come back with status=RESERVED, but only one of
    them should trigger real computation.

A release()d-then-successfully-retried reservation always ends
CONSUMED, never stranded in RELEASED -- consume()/release() act on the
same `id` regardless of whether it came from a fresh INSERT or a
RELEASED->RESERVED UPDATE.

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
# period already exhausted, so no row is inserted (or re-activated) at
# all.
DENIED = "DENIED"


@dataclass(frozen=True)
class Reservation:
    id: Optional[UUID]
    status: str  # RESERVED | CONSUMED | RELEASED | DENIED
    replay: bool  # True if this is an existing row returned via idempotent replay, not a fresh reservation
    # True only when THIS call performed a RELEASED -> RESERVED
    # transition (a legitimate retry the caller should proceed with).
    # False for a fresh reservation (replay is also False there) and
    # for a replay of an already-RESERVED/CONSUMED row (the caller
    # must NOT proceed with new computation in either of those cases).
    just_reactivated: bool = False
    attempt_count: int = 1


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
                "SELECT id, status, attempt_count FROM analysis_usage WHERE request_id = $1", request_id
            )
            if existing is not None:
                if existing["status"] in (RESERVED, CONSUMED):
                    # Already pending/in-flight, or already completed
                    # -- either way, this call creates nothing new.
                    return Reservation(
                        id=existing["id"], status=existing["status"], replay=True,
                        just_reactivated=False, attempt_count=existing["attempt_count"],
                    )

                # RELEASED: may be re-reserved, subject to the user's
                # *current* allowance -- a released slot does not
                # bypass the quota check.
                count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM analysis_usage
                    WHERE user_id = $1 AND period_key = $2 AND status IN ('RESERVED', 'CONSUMED')
                    """,
                    user_id, period_key,
                )
                if count >= allowance:
                    return Reservation(id=None, status=DENIED, replay=True, attempt_count=existing["attempt_count"])

                row = await conn.fetchrow(
                    """
                    UPDATE analysis_usage
                    SET status = 'RESERVED', reserved_at = now(),
                        consumed_at = NULL, released_at = NULL,
                        attempt_count = attempt_count + 1
                    WHERE id = $1
                    RETURNING id, status, attempt_count
                    """,
                    existing["id"],
                )
                return Reservation(
                    id=row["id"], status=row["status"], replay=True,
                    just_reactivated=True, attempt_count=row["attempt_count"],
                )

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
                    RETURNING id, status, attempt_count
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
                    "SELECT id, status, attempt_count FROM analysis_usage WHERE request_id = $1", request_id
                )
                return Reservation(
                    id=existing["id"], status=existing["status"], replay=True,
                    attempt_count=existing["attempt_count"],
                )

            return Reservation(id=row["id"], status=row["status"], replay=False, attempt_count=row["attempt_count"])


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
    frees the quota slot back up: RELEASED reservations are excluded
    from reserve()'s COUNT(*) check, so a released slot can be
    consumed again either by a new request_id, or by retrying the same
    request_id (Part II, Phase 14 -- reserve() now allows
    RELEASED -> RESERVED)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_usage SET status = 'RELEASED', released_at = now() "
                "WHERE id = $1 AND status = 'RESERVED'",
                reservation_id,
            )
