"""Idempotent RevenueCat webhook event processor -- the "idempotent
event processor -> local entitlement projection" stage of the required
flow (see BILLING_ARCHITECTURE.md). Runs off the queue
(app/workers/revenuecat_webhook_worker.py), never inline in the
webhook HTTP request.

Never calls the RevenueCat API -- everything here is a pure function
of one already-durably-received webhook_events row plus whatever is
already in user_entitlements. See
app/domain/revenuecat_reconciliation_service.py for the only module in
this pass that talks to RevenueCat's REST API.

Event-type handling matches REVENUECAT_INTEGRATION_NOTES.md section 3
exactly -- in particular: CANCELLATION does not revoke access (only
EXPIRATION does), except when cancel_reason=CUSTOMER_SUPPORT (a
refund), which does revoke immediately but does NOT fabricate
will_renew=False (refund and auto-renew-off are independent facts --
see _process_lifecycle_event below).

Two additional contracts this module enforces, both corrections found
by independent review of the original pass:

1. entitlement_ids gating (Section 4 of the billing brief / migration
   9815eb266923's docstring): a lifecycle event only mutates the
   locally-projected premium entitlement when
   settings.revenuecat_entitlement_id appears in the event's own
   entitlement_ids. An unrelated RevenueCat product/entitlement is a
   durably-received, successfully-processed NOT_RELEVANT outcome --
   never an error, never retried, never allowed to grant/revoke this
   app's premium access.
2. TRANSFER carries no app_user_id at all (transferred_from/
   transferred_to only, environment sometimes) -- see
   _process_transfer below and REVENUECAT_INTEGRATION_NOTES.md
   section 2.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

import asyncpg

from app.config import settings
from app.db import revenuecat_repository
from app.observability import events as observability_events

PROVIDER = "revenuecat"
TRANSFER_EVENT_TYPE = "TRANSFER"

_ENTITLEMENT_ACTIVE_EVENT_TYPES = {"INITIAL_PURCHASE", "RENEWAL", "PRODUCT_CHANGE", "UNCANCELLATION"}
# Every lifecycle event type whose semantics are actually about this
# app's one configured entitlement, and therefore must be checked
# against the event's own entitlement_ids before it may mutate
# anything. Everything else (VIRTUAL_CURRENCY_TRANSACTION,
# EXPERIMENT_ENROLLMENT, etc.) already falls through to the
# unconditional durable-receipt-only branch below and needs no gating.
_ENTITLEMENT_GATED_EVENT_TYPES = _ENTITLEMENT_ACTIVE_EVENT_TYPES | {"CANCELLATION", "EXPIRATION", "BILLING_ISSUE"}
_REFUND_CANCEL_REASON = "CUSTOMER_SUPPORT"


class WebhookEventNotFoundError(Exception):
    def __init__(self, webhook_event_id: UUID):
        self.webhook_event_id = webhook_event_id
        super().__init__(f"revenuecat_webhook_events row not found: {webhook_event_id}")


class UnresolvableAppUserIdError(Exception):
    """Raised for a lifecycle event whose app_user_id does not parse as
    a UUID, or parses but does not match any real users.id. Per Section
    11 of the brief ("do not trust arbitrary alias strings to map to
    application users without validation"), this is a hard failure,
    not a best-effort guess -- the event is marked FAILED, not silently
    dropped or applied against a fabricated identity.

    TRANSFER does NOT use this exception (see _process_transfer):
    an unresolvable transfer participant is a normal, expected
    occurrence (an alias that was never one of this app's own users)
    handled by resolving what can be resolved and failing closed via
    RECONCILIATION_REQUIRED when the destination itself can't be
    pinned down -- not a hard error."""

    def __init__(self, app_user_id: str):
        self.app_user_id = app_user_id
        super().__init__(f"app_user_id does not resolve to a real user: {app_user_id!r}")


@dataclass(frozen=True)
class _Outcome:
    """What process_webhook_event should record as this event's final
    processing_status, plus an optional machine-readable reason code
    (revenuecat_webhook_events.last_error_code -- a misnomer today for
    a non-error reason code, kept for schema compatibility)."""

    status: str
    error_code: Optional[str] = None


def _ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


async def _resolve_user(pool: asyncpg.Pool, app_user_id: Optional[str]) -> Optional[UUID]:
    """This app's own identity policy (Section 2) is that app_user_id
    IS the internal user UUID -- never email/username. So resolution is
    "parse as UUID, then confirm a real users row exists," never a
    lookup by any other field. Deliberately goes through the same
    set-current_user_id-then-select RLS path every other write in this
    codebase uses (see module docstring of app/db/usage_repository.py)
    rather than a superuser bypass -- there is no privileged read path
    for this table, and there doesn't need to be: the candidate id
    itself is the only thing being asked about."""
    if not app_user_id:
        return None
    try:
        candidate = UUID(app_user_id)
    except (ValueError, AttributeError, TypeError):
        return None

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(candidate))
            row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", candidate)
    return candidate if row is not None else None


def _emit_transition_event(result: revenuecat_repository.EntitlementProjection, event_type: str) -> None:
    if not result.applied:
        observability_events.revenuecat_event_stale(event_type=event_type)
        return
    if result.status == revenuecat_repository.ACTIVE:
        observability_events.entitlement_activated(event_type=event_type)
    elif result.status == revenuecat_repository.EXPIRED:
        observability_events.entitlement_expired(event_type=event_type)
    elif result.status == revenuecat_repository.REVOKED:
        observability_events.entitlement_revoked(event_type=event_type)


async def _process_lifecycle_event(
    pool: asyncpg.Pool, event: Dict[str, Any], event_type: str, environment: str, event_timestamp: datetime,
    revenuecat_event_id: str,
) -> _Outcome:
    if event_type in _ENTITLEMENT_GATED_EVENT_TYPES:
        entitlement_ids = event.get("entitlement_ids") or []
        if settings.revenuecat_entitlement_id not in entitlement_ids:
            # A real, durably-received event for a product/entitlement
            # this deployment does not treat as its premium
            # entitlement -- successfully processed as "nothing to do
            # here," never an error, never retried, and critically:
            # never allowed to touch this user's premium projection.
            return _Outcome(status=revenuecat_repository.NOT_RELEVANT)

    app_user_id = event.get("app_user_id")
    user_id = await _resolve_user(pool, app_user_id)
    if user_id is None:
        raise UnresolvableAppUserIdError(str(app_user_id))

    provider_customer_id = str(event.get("original_app_user_id") or app_user_id)
    expires_at = _ms_to_datetime(event.get("expiration_at_ms"))

    if event_type in _ENTITLEMENT_ACTIVE_EVENT_TYPES:
        status = revenuecat_repository.ACTIVE
        will_renew = True
    elif event_type == "CANCELLATION":
        if event.get("cancel_reason") == _REFUND_CANCEL_REASON:
            # A current-period refund -- revoke immediately, never wait
            # for the natural expiration. See
            # REVENUECAT_INTEGRATION_NOTES.md section 3. Refund and
            # auto-renew-off are independent facts: a refunded
            # subscription's renewal preference can still be *on*.
            # Preserve whatever was already projected rather than
            # fabricating will_renew=False -- the webhook payload does
            # not actually tell us the renewal setting changed. No
            # prior local row exists is the one case with nothing to
            # preserve; True (RevenueCat's own default posture for a
            # subscription that hasn't been explicitly cancelled) is
            # the honest default there, and it has no access
            # consequence anyway since status is REVOKED either way.
            existing = await revenuecat_repository.get_entitlement(
                pool, user_id, settings.revenuecat_entitlement_id, PROVIDER, environment,
            )
            status = revenuecat_repository.REVOKED
            will_renew = existing["will_renew"] if existing is not None else True
            expires_at = event_timestamp
        else:
            # Auto-renew turned off (or a billing-driven cancellation
            # notice) -- access is preserved through whatever
            # expiration was already projected; only will_renew flips.
            existing = await revenuecat_repository.get_entitlement(
                pool, user_id, settings.revenuecat_entitlement_id, PROVIDER, environment,
            )
            status = existing["status"] if existing is not None else revenuecat_repository.ACTIVE
            will_renew = False
            if existing is not None:
                expires_at = existing["expires_at"]
    elif event_type == "EXPIRATION":
        status = revenuecat_repository.EXPIRED
        will_renew = False
        expires_at = expires_at or event_timestamp
    elif event_type == "BILLING_ISSUE":
        # Not itself a revocation -- RevenueCat's own docs say so
        # explicitly. Enters GRACE_PERIOD, a distinct status from
        # ACTIVE precisely so a future policy could treat it
        # differently without conflating "definitely fine" with
        # "renewal attempt failed, might still recover."
        status = revenuecat_repository.GRACE_PERIOD
        will_renew = True
        expires_at = _ms_to_datetime(event.get("grace_period_expiration_at_ms")) or expires_at
    else:
        # An event type this pass deliberately does not project
        # (VIRTUAL_CURRENCY_TRANSACTION, EXPERIMENT_ENROLLMENT, etc.)
        # -- durable receipt already happened; there is nothing more
        # to do, and that is a successful, not a stale, outcome.
        return _Outcome(status=revenuecat_repository.PROCESSED)

    result = await revenuecat_repository.apply_entitlement_projection(
        pool, user_id=user_id, entitlement_identifier=settings.revenuecat_entitlement_id,
        provider=PROVIDER, status=status, effective_at=event_timestamp, expires_at=expires_at,
        will_renew=will_renew, environment=environment, source_event_id=revenuecat_event_id,
        provider_customer_id=provider_customer_id, last_provider_event_at=event_timestamp,
    )
    _emit_transition_event(result, event_type)
    return _Outcome(status=revenuecat_repository.PROCESSED if result.applied else revenuecat_repository.STALE_IGNORED)


async def _process_transfer(
    pool: asyncpg.Pool, event: Dict[str, Any], environment: Optional[str], event_timestamp: datetime,
    revenuecat_event_id: str,
) -> _Outcome:
    """TRANSFER carries transferred_from/transferred_to (both always
    present -- enforced by the webhook route's own validation before
    this event was ever durably stored), never app_user_id. environment
    is only SOMETIMES present on this event type per RevenueCat's own
    docs -- see REVENUECAT_INTEGRATION_NOTES.md section 2.

    Resolution policy (Section 11 -- never trust an arbitrary alias
    string): every transferred_to/transferred_from entry is resolved
    independently; unresolvable entries are simply not local users and
    are skipped, never treated as a hard failure and never fabricated
    into a user. Exactly one distinct resolvable local destination is
    required to proceed -- zero or more than one both fail closed via
    RECONCILIATION_REQUIRED rather than guessing, so this transfer can
    never grant premium access to multiple unrelated local users, and
    can never silently do nothing while pretending it succeeded.

    Ordering (fail-safe): every resolvable source is revoked before the
    destination is activated, so a partial failure between the two
    steps favors temporary denial over duplicate paid entitlement.
    """
    if environment is None:
        return _Outcome(status=revenuecat_repository.RECONCILIATION_REQUIRED, error_code="UNRESOLVED_ENVIRONMENT")

    transferred_to: List[str] = event.get("transferred_to") or []
    transferred_from: List[str] = event.get("transferred_from") or []

    resolved_destinations: Set[UUID] = set()
    for raw in transferred_to:
        resolved = await _resolve_user(pool, raw)
        if resolved is not None:
            resolved_destinations.add(resolved)

    if len(resolved_destinations) == 0:
        return _Outcome(
            status=revenuecat_repository.RECONCILIATION_REQUIRED,
            error_code="NO_RESOLVABLE_TRANSFER_DESTINATION",
        )
    if len(resolved_destinations) > 1:
        return _Outcome(
            status=revenuecat_repository.RECONCILIATION_REQUIRED,
            error_code="AMBIGUOUS_TRANSFER_DESTINATION",
        )
    destination_id = next(iter(resolved_destinations))

    resolved_sources: Set[UUID] = set()
    for raw in transferred_from:
        resolved = await _resolve_user(pool, raw)
        if resolved is not None and resolved != destination_id:
            # Unresolvable, or a self-transfer -- nothing to revoke.
            # An unresolvable source most likely means that alias was
            # never one of this app's own users to begin with; the
            # destination side (never duplicating paid access) is the
            # side this app's brief requires to be correct above all
            # else.
            resolved_sources.add(resolved)

    expires_at = _ms_to_datetime(event.get("expiration_at_ms"))
    any_applied = False

    for source_id in resolved_sources:
        result = await revenuecat_repository.apply_entitlement_projection(
            pool, user_id=source_id, entitlement_identifier=settings.revenuecat_entitlement_id,
            provider=PROVIDER, status=revenuecat_repository.REVOKED, effective_at=event_timestamp,
            expires_at=event_timestamp, will_renew=False, environment=environment,
            source_event_id=revenuecat_event_id, provider_customer_id=str(source_id),
            last_provider_event_at=event_timestamp,
        )
        _emit_transition_event(result, "TRANSFER_SOURCE_REVOKED")
        any_applied = any_applied or result.applied

    dest_result = await revenuecat_repository.apply_entitlement_projection(
        pool, user_id=destination_id, entitlement_identifier=settings.revenuecat_entitlement_id,
        provider=PROVIDER, status=revenuecat_repository.ACTIVE, effective_at=event_timestamp,
        expires_at=expires_at, will_renew=True, environment=environment,
        source_event_id=revenuecat_event_id, provider_customer_id=str(destination_id),
        last_provider_event_at=event_timestamp,
    )
    _emit_transition_event(dest_result, "TRANSFER_DESTINATION_ACTIVATED")
    any_applied = any_applied or dest_result.applied

    return _Outcome(status=revenuecat_repository.PROCESSED if any_applied else revenuecat_repository.STALE_IGNORED)


async def process_webhook_event(pool: asyncpg.Pool, webhook_event_id: UUID) -> None:
    """Called by app/workers/revenuecat_webhook_worker.py for each
    claimed `revenuecat_webhook` job. Re-running this for an
    already-PROCESSED event (a reclaimed/duplicate job -- see the
    webhook route's idempotent enqueue-by-event-id) is a safe no-op."""
    event_row = await revenuecat_repository.get_event(pool, webhook_event_id)
    if event_row is None:
        raise WebhookEventNotFoundError(webhook_event_id)

    if event_row["processing_status"] == revenuecat_repository.PROCESSED:
        return

    raw_payload = event_row["payload_json"]
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    event = payload.get("event", payload)
    event_type = event_row["event_type"]
    environment = event_row["environment"]  # may be None for a TRANSFER RevenueCat sent with no environment
    event_timestamp = event_row["event_timestamp"]
    revenuecat_event_id = event_row["revenuecat_event_id"]

    try:
        if event_type == TRANSFER_EVENT_TYPE:
            outcome = await _process_transfer(pool, event, environment, event_timestamp, revenuecat_event_id)
        else:
            outcome = await _process_lifecycle_event(
                pool, event, event_type, environment, event_timestamp, revenuecat_event_id,
            )
    except UnresolvableAppUserIdError:
        await revenuecat_repository.mark_event_result(
            pool, webhook_event_id, status=revenuecat_repository.FAILED, error_code="UNKNOWN_APP_USER_ID",
        )
        observability_events.revenuecat_event_failed(event_type=event_type, error_code="UNKNOWN_APP_USER_ID")
        return

    await revenuecat_repository.mark_event_result(
        pool, webhook_event_id, status=outcome.status, error_code=outcome.error_code,
    )
    if outcome.status == revenuecat_repository.PROCESSED:
        observability_events.revenuecat_event_processed(event_type=event_type)
    elif outcome.status == revenuecat_repository.RECONCILIATION_REQUIRED:
        observability_events.revenuecat_event_requires_reconciliation(
            event_type=event_type, error_code=outcome.error_code or "UNKNOWN",
        )
    elif outcome.status == revenuecat_repository.NOT_RELEVANT:
        observability_events.revenuecat_event_not_relevant(event_type=event_type)
