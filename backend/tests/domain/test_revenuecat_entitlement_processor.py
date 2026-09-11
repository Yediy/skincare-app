"""app.domain.revenuecat_entitlement_processor -- event-type semantics,
out-of-order protection, transfer/alias handling. Runs against real
Postgres (app_db_pool), same convention as tests/domain/test_entitlement.py.

Every test drives the processor exactly the way
app/workers/revenuecat_webhook_worker.py does: record_event() (the
durable-receipt half a real webhook delivery would have already done)
then process_webhook_event() -- never calling internal per-event-type
helpers directly, so these tests exercise the real, whole path.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db import revenuecat_repository
from app.domain.revenuecat_entitlement_processor import process_webhook_event

ENTITLEMENT_ID = "premium"


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def _record_and_process(
    app_db_pool, *, event_type, app_user_id, event_timestamp, environment="PRODUCTION",
    event_id=None, extra_fields=None,
):
    event_id = event_id or str(uuid.uuid4())
    event = {
        "id": event_id,
        "type": event_type,
        "app_user_id": str(app_user_id),
        "environment": environment,
        "event_timestamp_ms": _ms(event_timestamp),
        **(extra_fields or {}),
    }
    payload = {"api_version": "1.0", "event": event}
    recorded = await revenuecat_repository.record_event(
        app_db_pool,
        revenuecat_event_id=event_id,
        event_type=event_type,
        app_user_id=str(app_user_id),
        environment=environment,
        event_timestamp=event_timestamp,
        payload_json=payload,
    )
    await process_webhook_event(app_db_pool, recorded.id)
    return recorded


async def _get(app_db_pool, user_id, environment="PRODUCTION"):
    return await revenuecat_repository.get_entitlement(app_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", environment)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def test_initial_purchase_activates_entitlement(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-initial@test.com")
    expires = T0 + timedelta(days=30)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "ACTIVE"
    assert row["will_renew"] is True
    assert row["expires_at"] == expires


async def test_renewal_extends_entitlement(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-renewal@test.com")
    t1_expires = T0 + timedelta(days=30)
    t2 = T0 + timedelta(days=29)
    t2_expires = T0 + timedelta(days=59)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(t1_expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="RENEWAL", app_user_id=user_id, event_timestamp=t2,
        extra_fields={"expiration_at_ms": _ms(t2_expires)},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "ACTIVE"
    assert row["expires_at"] == t2_expires


async def test_normal_cancellation_retains_access_through_expiration(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-cancel-normal@test.com")
    expires = T0 + timedelta(days=30)
    t2 = T0 + timedelta(days=10)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="CANCELLATION", app_user_id=user_id, event_timestamp=t2,
        extra_fields={"cancel_reason": "UNSUBSCRIBE", "expiration_at_ms": _ms(expires)},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "ACTIVE"  # access preserved
    assert row["will_renew"] is False
    assert row["expires_at"] == expires  # unchanged


async def test_uncancellation_restores_renewal(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-uncancel@test.com")
    expires = T0 + timedelta(days=30)
    t2 = T0 + timedelta(days=10)
    t3 = T0 + timedelta(days=15)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="CANCELLATION", app_user_id=user_id, event_timestamp=t2,
        extra_fields={"cancel_reason": "UNSUBSCRIBE"},
    )
    await _record_and_process(
        app_db_pool, event_type="UNCANCELLATION", app_user_id=user_id, event_timestamp=t3,
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "ACTIVE"
    assert row["will_renew"] is True


async def test_expiration_removes_access(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-expire@test.com")
    expires = T0 + timedelta(days=30)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="EXPIRATION", app_user_id=user_id, event_timestamp=expires,
        extra_fields={"expiration_reason": "UNSUBSCRIBE"},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "EXPIRED"
    assert row["will_renew"] is False


async def test_billing_issue_enters_grace_period_not_revoked(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-billing-issue@test.com")
    expires = T0 + timedelta(days=30)
    t2 = T0 + timedelta(days=29)
    grace_expiry = T0 + timedelta(days=32)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="BILLING_ISSUE", app_user_id=user_id, event_timestamp=t2,
        extra_fields={"grace_period_expiration_at_ms": _ms(grace_expiry)},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "GRACE_PERIOD"
    assert row["expires_at"] == grace_expiry


async def test_refund_cancellation_revokes_immediately(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-refund@test.com")
    expires = T0 + timedelta(days=30)
    t2 = T0 + timedelta(days=5)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="CANCELLATION", app_user_id=user_id, event_timestamp=t2,
        extra_fields={"cancel_reason": "CUSTOMER_SUPPORT"},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "REVOKED"
    assert row["expires_at"] == t2
    assert row["will_renew"] is False


async def test_out_of_order_stale_event_does_not_override_newer_state(db_pool, app_db_pool):
    """RENEWAL at T2 processed first; a CANCELLATION timestamped
    earlier (T1) arrives afterward -- it must not revert will_renew or
    the extended expiry T2 already established."""
    user_id = await _create_user(db_pool, "rc-out-of-order@test.com")
    t1 = T0
    t2 = T0 + timedelta(days=29)
    t2_expires = T0 + timedelta(days=59)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=t1,
        extra_fields={"expiration_at_ms": _ms(T0 + timedelta(days=30))},
    )
    await _record_and_process(
        app_db_pool, event_type="RENEWAL", app_user_id=user_id, event_timestamp=t2,
        extra_fields={"expiration_at_ms": _ms(t2_expires)},
    )
    stale_event = await _record_and_process(
        app_db_pool, event_type="CANCELLATION", app_user_id=user_id, event_timestamp=t1,
        extra_fields={"cancel_reason": "UNSUBSCRIBE"},
    )

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "ACTIVE"
    assert row["will_renew"] is True  # NOT flipped false by the stale cancellation
    assert row["expires_at"] == t2_expires  # NOT reverted

    stale_row = await revenuecat_repository.get_event(app_db_pool, stale_event.id)
    assert stale_row["processing_status"] == "STALE_IGNORED"


async def test_transfer_revokes_source_and_activates_destination(db_pool, app_db_pool):
    source_id = await _create_user(db_pool, "rc-transfer-source@test.com")
    dest_id = await _create_user(db_pool, "rc-transfer-dest@test.com")
    expires = T0 + timedelta(days=30)
    t2 = T0 + timedelta(days=1)

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=source_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(expires)},
    )
    await _record_and_process(
        app_db_pool, event_type="TRANSFER", app_user_id=dest_id, event_timestamp=t2,
        extra_fields={"transferred_from": [str(source_id)], "expiration_at_ms": _ms(expires)},
    )

    source_row = await _get(app_db_pool, source_id)
    dest_row = await _get(app_db_pool, dest_id)
    assert source_row["status"] == "REVOKED"
    assert dest_row["status"] == "ACTIVE"


async def test_unresolvable_app_user_id_fails_closed_without_creating_any_entitlement(db_pool, app_db_pool):
    """Section 11: never trust an arbitrary id string. A random UUID
    that doesn't match a real user must not fabricate an entitlement
    row for anyone."""
    unknown_user_id = uuid.uuid4()

    recorded = await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=unknown_user_id, event_timestamp=T0,
    )

    event_row = await revenuecat_repository.get_event(app_db_pool, recorded.id)
    assert event_row["processing_status"] == "FAILED"
    assert event_row["last_error_code"] == "UNKNOWN_APP_USER_ID"

    count = await db_pool.fetchval("SELECT COUNT(*) FROM user_entitlements")
    assert count == 0


async def test_sandbox_and_production_entitlements_are_tracked_separately(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-sandbox-scope@test.com")

    await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        environment="SANDBOX", extra_fields={"expiration_at_ms": _ms(T0 + timedelta(days=30))},
    )

    sandbox_row = await _get(app_db_pool, user_id, environment="SANDBOX")
    production_row = await _get(app_db_pool, user_id, environment="PRODUCTION")
    assert sandbox_row["status"] == "ACTIVE"
    assert production_row is None


async def test_reprocessing_an_already_processed_event_is_a_safe_no_op(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rc-reprocess@test.com")
    recorded = await _record_and_process(
        app_db_pool, event_type="INITIAL_PURCHASE", app_user_id=user_id, event_timestamp=T0,
        extra_fields={"expiration_at_ms": _ms(T0 + timedelta(days=30))},
    )

    await process_webhook_event(app_db_pool, recorded.id)  # second run, same event

    row = await _get(app_db_pool, user_id)
    assert row["status"] == "ACTIVE"
