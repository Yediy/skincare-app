"""Consumes `revenuecat_webhook` jobs enqueued by
app/api/v2/webhooks.py, running
app/domain/revenuecat_entitlement_processor.py::process_webhook_event
for each one. Entry point:

    python -m app.workers.revenuecat_webhook_worker

Same claim -> process -> acknowledge/fail(retryable) shape as
app/workers/analysis_worker.py, deliberately without a heartbeat loop:
unlike a CV pipeline run, processing one webhook event is a handful of
fast Postgres statements, well inside the default claim visibility
window, so there is no long-running claim to protect against
reclaiming.
"""
import asyncio
import logging

from uuid import UUID

from app.domain.revenuecat_entitlement_processor import WebhookEventNotFoundError, process_webhook_event
from app.observability import events as observability_events
from app.queue.base import Job, JobNotFoundError, JobQueue

logger = logging.getLogger(__name__)

REVENUECAT_WEBHOOK_JOB_TYPE = "revenuecat_webhook"
CLAIM_VISIBILITY_TIMEOUT_SECONDS = 60
EMPTY_QUEUE_POLL_INTERVAL_SECONDS = 2.0


async def _process_job(job: Job, job_queue: JobQueue, pool) -> None:
    webhook_event_id = UUID(job.payload["webhook_event_id"])
    try:
        await process_webhook_event(pool, webhook_event_id)
        await job_queue.acknowledge(job.id)
        logger.info("revenuecat_webhook_worker: job=%s webhook_event_id=%s completed", job.id, webhook_event_id)
    except WebhookEventNotFoundError:
        # The referenced revenuecat_webhook_events row doesn't exist --
        # cannot happen for a job this route itself just enqueued
        # inside the same transaction as the row insert, so retrying
        # will not change the outcome.
        await job_queue.fail(job.id, "WEBHOOK_EVENT_NOT_FOUND", retryable=False)
        logger.error("revenuecat_webhook_worker: job=%s webhook_event_id=%s not found", job.id, webhook_event_id)
    except Exception as e:
        is_terminal = await job_queue.fail(job.id, f"{e.__class__.__name__}", retryable=True)
        logger.warning(
            "revenuecat_webhook_worker: job=%s webhook_event_id=%s failed terminal=%s",
            job.id, webhook_event_id, is_terminal, exc_info=True,
        )
        if is_terminal:
            observability_events.revenuecat_event_failed(event_type="UNKNOWN", error_code="PROCESSING_EXHAUSTED")


async def run_one_cycle(job_queue: JobQueue, pool) -> bool:
    job = await job_queue.claim(REVENUECAT_WEBHOOK_JOB_TYPE, visibility_timeout_seconds=CLAIM_VISIBILITY_TIMEOUT_SECONDS)
    if job is None:
        return False
    await _process_job(job, job_queue, pool)
    return True


async def run_forever(job_queue: JobQueue, pool, *, poll_interval_seconds: float = EMPTY_QUEUE_POLL_INTERVAL_SECONDS) -> None:
    while True:
        claimed = await run_one_cycle(job_queue, pool)
        if not claimed:
            await asyncio.sleep(poll_interval_seconds)


async def _main() -> None:
    from app.db.connection import init_db_pool, get_db_pool, close_db_pool
    from app.queue.postgres_queue import PostgresJobQueue

    await init_db_pool()
    try:
        pool = get_db_pool()
        job_queue = PostgresJobQueue(pool)
        logger.info("revenuecat_webhook_worker: starting main loop")
        await run_forever(job_queue, pool)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
