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
"""
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from app.config import settings


@dataclass(frozen=True)
class UserPlacement:
    home_region: str
    cell_id: str


class UserPlacementService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get_placement(self, user_id: UUID) -> UserPlacement:
        """Idempotent assign-if-unset, in one round trip:
        COALESCE(existing, launch default) -- a user who already has a
        placement keeps it unchanged forever; a user who has none yet
        (created before this pass, or never assigned) is durably
        assigned the deployment's current launch defaults exactly
        once, on this first call, not re-derived per call."""
        row = await self._pool.fetchrow(
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
            # No such user -- honest fallback to the launch defaults
            # rather than raising; callers that need "does this user
            # exist" already have their own check upstream (auth).
            return UserPlacement(home_region=settings.launch_home_region, cell_id=settings.launch_cell_id)
        return UserPlacement(home_region=row["home_region"], cell_id=row["cell_id"])
