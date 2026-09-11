"""UserPlacementService (Part VII, minimal cell readiness): the
deployment's configured launch home_region/cell_id (settings.
launch_home_region/launch_cell_id -- never a literal hardcoded here or
at any other call site), assigned to a user the first time anything
asks for their placement, and otherwise just read back unchanged.

Deliberately not multi-region infrastructure: nothing here routes a
request differently based on region/cell, replicates data across
regions, or shards a datastore. It exists purely so
analysis_requests.home_region/cell_id (migration b034483cb876) has a
real, non-fabricated value to snapshot at submission time -- see
AnalysisSubmissionService.submit()'s `region` parameter and
app/api/v2/analyses.py's POST route -- and so that snapshot survives
unchanged even if a user's own placement is reassigned later (a future
concern this pass does not need to solve: today's assignment never
changes once set).

`users` has row-level security (migration feb038fd05bd, corrected P0-1
-- an earlier draft of this module wrongly assumed `users` carried no
RLS at all): the `users_identity_scope` policy requires
`app.current_user_id` to equal the row's own `id` for any UPDATE (both
USING and WITH CHECK), same pattern every other RLS-protected repository
in this codebase already uses (see app/db/profile_repository.py). The
UPDATE below therefore runs inside an explicit transaction that sets
that GUC, via set_config(), to the exact user_id being placed -- to
$1's own id, never a caller-supplied "target" distinct from it, so this
can never be used to assign or read another user's placement.
"""
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from app.config import settings


@dataclass(frozen=True)
class UserPlacement:
    home_region: str
    cell_id: str


class UserPlacementNotFoundError(Exception):
    """Raised when the UPDATE ... RETURNING finds no row for user_id,
    correctly scoped by RLS to that same user_id. get_placement() is
    only ever called after authentication (see app/api/v2/analyses.py),
    so a real caller reaching this means the authenticated user's own
    row is unexpectedly gone -- deleted between auth and this call, or
    a genuine bug upstream. Either way this is a fail-closed signal,
    never silently papered over by returning the deployment's launch
    defaults as if they had been durably assigned."""


class UserPlacementService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get_placement(self, user_id: UUID) -> UserPlacement:
        """Idempotent assign-if-unset, in one round trip:
        COALESCE(existing, launch default) -- a user who already has a
        placement keeps it unchanged forever; a user who has none yet
        (created before this pass, or never assigned) is durably
        assigned the deployment's current launch defaults exactly
        once, on this first call, not re-derived per call.

        Runs inside an explicit transaction with app.current_user_id
        set to this exact user_id as its first statement -- without
        that, `users_identity_scope`'s WITH CHECK rejects every row
        (RLS fails closed, not open), so the UPDATE would silently
        affect zero rows instead of actually persisting anything."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', $1, true)", str(user_id)
                )
                row = await conn.fetchrow(
                    """
                    UPDATE users
                    SET home_region = COALESCE(home_region, $2),
                        cell_id = COALESCE(cell_id, $3)
                    WHERE id = $1
                    RETURNING home_region, cell_id
                    """,
                    user_id, settings.launch_home_region, settings.launch_cell_id,
                )
        if row is None:
            raise UserPlacementNotFoundError(str(user_id))
        return UserPlacement(home_region=row["home_region"], cell_id=row["cell_id"])
