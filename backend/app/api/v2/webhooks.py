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

Required-field validation is event-type-aware (REVENUECAT_INTEGRATION_
NOTES.md section 2): `id`, `type`, `event_timestamp_ms` are the only
fields genuinely common to every RevenueCat event. `TRANSFER` does not
carry `app_user_id` at all -- it carries `transferred_from`/
`transferred_to` (always) and `environment` (sometimes) -- so it gets
its own validation branch rather than being forced through the
app_user_id/environment-required path every other (lifecycle) event
type uses. A real TRANSFER delivery with no `environment` is still
accepted and durably stored (never rejected merely for omitting an
optional RevenueCat field, never fabricated) -- see
app/domain/revenuecat_entitlement_processor.py for how a missing
environment is resolved during processing (RECONCILIATION_REQUIRED,
not silently guessed).

Uses the dedicated billing pool (get_billing_db_pool -- the
skincare_billing role, migration 9815eb266923), not the ordinary
runtime pool: durably recording a webhook event and enqueuing its job
requires write privilege the ordinary skincare_app role no longer has.
See BILLING_ARCHITECTURE.md's "Database privilege boundary" section.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db import revenuecat_repository
from app.db.connection import get_billing_db_pool
from app.observability import events as observability_events
from app.queue.postgres_queue import PostgresJobQueue
from app.security.revenuecat_webhook import WebhookVerificationError, verify_webhook_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/webhooks", tags=["webhooks"])

REVENUECAT_WEBHOOK_JOB_TYPE = "revenuecat_webhook"

TRANSFER_EVENT_TYPE = "TRANSFER"


def _validate_event_fields(event: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Returns (is_valid, rejection_reason). Field requirements are
    genuinely event-type-aware, not a single all-events-require-
    everything check -- see module docstring."""
    if not event.get("id") or not event.get("type") or not event.get("event_timestamp_ms"):
        return False, "MISSING_REQUIRED_FIELDS"

    if event.get("type") == TRANSFER_EVENT_TYPE:
        transferred_from = event.get("transferred_from")
        transferred_to = event.get("transferred_to")
        if not transferred_from or not transferred_to:
            return False, "MISSING_REQUIRED_TRANSFER_FIELDS"
        return True, None

    # Every other (lifecycle/purchase) event type: app_user_id and
    # environment are documented as always present.
    if not event.get("app_user_id") or not event.get("environment"):
        return False, "MISSING_REQUIRED_FIELDS"
    return True, None


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
    app_user_id = event.get("app_user_id")  # None for TRANSFER -- never required
    environment = event.get("environment")  # may legitimately be None for TRANSFER
    event_timestamp_ms = event.get("event_timestamp_ms")

    is_valid, rejection_reason = _validate_event_fields(event)
    if not is_valid:
        # A well-formed-JSON but not-a-real-RevenueCat-event body --
        # still logged/rejected honestly rather than durably stored as
        # if it were a real event with fabricated defaults.
        observability_events.revenuecat_webhook_rejected(reason=rejection_reason)
        raise HTTPException(status_code=400, detail="Missing required event fields")

    event_timestamp = datetime.fromtimestamp(event_timestamp_ms / 1000, tz=timezone.utc)

    pool = get_billing_db_pool()
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
                app_user_id=str(app_user_id) if app_user_id is not None else None,
                environment=str(environment) if environment is not None else None,
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
