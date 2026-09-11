"""POST /api/v2/webhooks/revenuecat -- the only HTTP entry point for
RevenueCat billing events. Deliberately NOT behind get_current_user or
rate_limit_by_user/rate_limit_by_ip (see module docstring below) --
RevenueCat is not one of this application's own authenticated users,
and ordinary per-user/per-IP rate limiting would let RevenueCat's own
retry behavior (up to 5 redeliveries per event -- see
REVENUECAT_INTEGRATION_NOTES.md section 1) collide with limits sized
for human traffic.

Sequence, per Section 6 of the billing brief: verify -> durable event
insert -> enqueue a `revenuecat_webhook` job -> 200. No entitlement
logic runs inline in this request -- see
app/domain/revenuecat_entitlement_processor.py, invoked only by
app/workers/revenuecat_webhook_worker.py.
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db import revenuecat_repository
from app.db.connection import get_db_pool
from app.observability import events as observability_events
from app.queue.postgres_queue import PostgresJobQueue
from app.security.revenuecat_webhook import WebhookVerificationError, verify_webhook_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/webhooks", tags=["webhooks"])

REVENUECAT_WEBHOOK_JOB_TYPE = "revenuecat_webhook"


@router.post("/revenuecat", status_code=200)
async def revenuecat_webhook(request: Request):
    raw_body = await request.body()

    try:
        verify_webhook_request(
            raw_body,
            request.headers.get("authorization"),
            request.headers.get("x-revenuecat-webhook-signature"),
        )
    except WebhookVerificationError as e:
        observability_events.revenuecat_webhook_rejected(reason=e.code)
        raise HTTPException(status_code=401, detail="Webhook verification failed")

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        observability_events.revenuecat_webhook_rejected(reason="MALFORMED_BODY")
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    event = body.get("event", body)
    event_id = event.get("id")
    event_type = event.get("type")
    app_user_id = event.get("app_user_id")
    environment = event.get("environment")
    event_timestamp_ms = event.get("event_timestamp_ms")

    if not all([event_id, event_type, app_user_id, environment, event_timestamp_ms]):
        # A well-formed-JSON but not-a-real-RevenueCat-event body --
        # still logged/rejected honestly rather than durably stored as
        # if it were a real event with fabricated defaults.
        observability_events.revenuecat_webhook_rejected(reason="MISSING_REQUIRED_FIELDS")
        raise HTTPException(status_code=400, detail="Missing required event fields")

    event_timestamp = datetime.fromtimestamp(event_timestamp_ms / 1000, tz=timezone.utc)

    pool = get_db_pool()
    job_queue = PostgresJobQueue(pool)

    # Durable insert + enqueue in one transaction: either both commit
    # or neither does, same "atomic DB enqueue" pattern as
    # AnalysisSubmissionService.submit(). Idempotency is layered twice,
    # deliberately: revenuecat_webhook_events.revenuecat_event_id is
    # UNIQUE (the durable-receipt guarantee), and the job is enqueued
    # with request_id=event_id (the job queue's own
    # (job_type, request_id) uniqueness) -- so 10 concurrent deliveries
    # of the same event produce exactly one event row and exactly one
    # job, regardless of which of the two uniqueness constraints a
    # given concurrent caller happens to race through.
    async with pool.acquire() as conn:
        async with conn.transaction():
            recorded = await revenuecat_repository.record_event(
                pool,
                revenuecat_event_id=str(event_id),
                event_type=str(event_type),
                app_user_id=str(app_user_id),
                environment=str(environment),
                event_timestamp=event_timestamp,
                payload_json=body,
                conn=conn,
            )
            await job_queue.enqueue(
                REVENUECAT_WEBHOOK_JOB_TYPE,
                {"webhook_event_id": str(recorded.id)},
                request_id=f"revenuecat:{event_id}",
                conn=conn,
            )

    if recorded.is_new:
        observability_events.revenuecat_webhook_received(event_type=str(event_type))
    else:
        observability_events.revenuecat_event_duplicate(event_type=str(event_type))

    return {"status": "received"}
