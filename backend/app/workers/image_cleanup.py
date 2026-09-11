"""Part IV, Phase 22's safety-net cleanup mechanism. The PRIMARY
deletion path is the CV worker itself (app/workers/analysis_worker.py)
deleting its own image immediately after terminal processing. This
module is the safety net for what that primary path can't cover: a
worker crash, a killed process, a network failure, or an R2 timeout
between "processing finished" and "delete call completed" -- an
application-level delete call alone does not protect against any of
those, so a second, independent mechanism (this sweeper, driven by the
DB-tracked image_expires_at rather than the worker's own control flow)
is required, not merely nice to have.

Intended to run periodically (a scheduled job/cron -- how that
scheduling itself is wired up is a deployment concern documented in
WORKER_OPERATIONS.md, not this module's job) via `python -m
app.workers.image_cleanup`.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List

import asyncpg

from app.db import analysis_repository
from app.observability import events as observability_events
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

logger = logging.getLogger(__name__)


@dataclass
class CleanupSweepResult:
    attempted: int = 0
    deleted: int = 0
    failed: int = 0
    failed_request_ids: List[str] = field(default_factory=list)


async def run_cleanup_sweep(
    pool: asyncpg.Pool,
    image_store: EphemeralAnalysisImageStore,
    *,
    batch_limit: int = 100,
) -> CleanupSweepResult:
    """One sweep: finds overdue images (across every user --
    find_overdue_ephemeral_images() is a SECURITY DEFINER function
    exactly for this cross-user need), attempts to delete each, and
    only clears the DB-side reference once deletion is *confirmed*
    (delete_if_confirmed() returning True) -- so a storage-unavailable
    failure leaves the reference in place for the next sweep to retry,
    rather than silently losing track of an orphaned object.

    Tracks failures explicitly (Phase 22's own requirement) rather than
    only logging and moving on -- the returned CleanupSweepResult is
    what Part VIII's metrics layer (raw-image cleanup failures) reads.
    """
    result = CleanupSweepResult()
    overdue = await analysis_repository.find_overdue_ephemeral_images(pool, batch_limit=batch_limit)

    for row in overdue:
        result.attempted += 1
        confirmed = await image_store.delete_if_confirmed(row["image_object_key"])
        if confirmed:
            await analysis_repository.clear_image_reference(pool, row["id"])
            result.deleted += 1
        else:
            result.failed += 1
            result.failed_request_ids.append(str(row["id"]))
            observability_events.image_cleanup_failure(analysis_request_id=str(row["id"]))
            logger.warning(
                "image_cleanup: failed to confirm deletion for analysis_request_id=%s (object storage unreachable, will retry next sweep)",
                row["id"],
            )

    return result


async def _main() -> None:
    """Real entry point: `python -m app.workers.image_cleanup`. Not
    itself a scheduler -- run this on whatever cadence the deployment's
    job scheduler configures (see WORKER_OPERATIONS.md)."""
    from app.config import settings
    from app.db.connection import init_db_pool, get_db_pool, close_db_pool
    from app.storage.r2 import CloudflareR2ObjectStorage

    await init_db_pool()
    try:
        object_storage = CloudflareR2ObjectStorage(
            account_id=settings.r2_account_id,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
            bucket=settings.r2_bucket,
            endpoint_url=settings.r2_endpoint,
        )
        image_store = EphemeralAnalysisImageStore(object_storage)
        result = await run_cleanup_sweep(get_db_pool(), image_store)
        logger.info(
            "image_cleanup: sweep complete -- attempted=%d deleted=%d failed=%d",
            result.attempted, result.deleted, result.failed,
        )
    finally:
        await close_db_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
