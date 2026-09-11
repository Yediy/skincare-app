"""POST /api/v2/webhooks/revenuecat -- end-to-end through the real
HTTP path (real Postgres, real signature verification), same
`client` fixture as tests/api/test_analyses_v2.py. Never calls the
processor/worker directly -- these tests are about the route's own
verify -> durable-insert -> enqueue -> 200 contract, including
concurrency and event-type-aware required-field validation, not about
entitlement projection semantics (see
tests/domain/test_revenuecat_entitlement_processor.py for that).

Durable-row assertions go through billing_db_pool (the skincare_billing
role), not app_db_pool -- the ordinary skincare_app role has no read
grant on revenuecat_webhook_events at all after migration
9815eb266923 (see tests/database/test_revenuecat_billing_privilege.py).
`jobs` assertions stay on app_db_pool since skincare_app keeps its
pre-existing SELECT grant on that table.
"""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest

from app.config import settings

AUTH_VALUE = "test-only-webhook-route-auth-value-never-used-outside-pytest-0123456789"
SIGNING_SECRET = "test-only-webhook-route-signing-secret-never-used-outside-pytest-0123456789"


def _sign(body: bytes, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture(autouse=True)
def _configure_webhook_secrets(monkeypatch):
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", AUTH_VALUE)
    monkeypatch.setattr(settings, "revenuecat_webhook_signing_secret", SIGNING_SECRET)


def _event_body(event_id: str, event_type: str = "INITIAL_PURCHASE", **extra) -> bytes:
    event = {
        "id": event_id,
        "type": event_type,
        "app_user_id": str(uuid.uuid4()),
        "environment": "PRODUCTION",
        "event_timestamp_ms": int(time.time() * 1000),
        **extra,
    }
    payload = {"api_version": "1.0", "event": event}
    return json.dumps(payload).encode("utf-8")


def _transfer_body(event_id: str, *, transferred_from, transferred_to, environment="PRODUCTION") -> bytes:
    """RevenueCat's ACTUAL documented TRANSFER shape -- no app_user_id
    at all. See REVENUECAT_INTEGRATION_NOTES.md section 2."""
    event = {
        "id": event_id,
        "type": "TRANSFER",
        "event_timestamp_ms": int(time.time() * 1000),
        "transferred_from": transferred_from,
        "transferred_to": transferred_to,
    }
    if environment is not None:
        event["environment"] = environment
    payload = {"api_version": "1.0", "event": event}
    return json.dumps(payload).encode("utf-8")


def _headers(body: bytes) -> dict:
    now = int(time.time())
    return {
        "Authorization": AUTH_VALUE,
        "X-RevenueCat-Webhook-Signature": _sign(body, SIGNING_SECRET, now),
        "Content-Type": "application/json",
    }


async def test_valid_webhook_accepted_creates_durable_event_and_job(client, billing_db_pool, app_db_pool):
    event_id = str(uuid.uuid4())
    body = _event_body(event_id)

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert response.status_code == 200

    event_row = await billing_db_pool.fetchrow(
        "SELECT * FROM revenuecat_webhook_events WHERE revenuecat_event_id = $1", event_id
    )
    assert event_row is not None
    assert event_row["event_type"] == "INITIAL_PURCHASE"

    job_row = await app_db_pool.fetchrow(
        "SELECT * FROM jobs WHERE job_type = 'revenuecat_webhook' AND request_id = $1", f"revenuecat:{event_id}"
    )
    assert job_row is not None


async def test_real_revenuecat_transfer_shape_with_no_app_user_id_is_accepted(client, billing_db_pool):
    """The actual documented TRANSFER payload shape: no app_user_id,
    only transferred_from/transferred_to (+ optional environment).
    Must be accepted, not rejected as missing a field it never had."""
    event_id = str(uuid.uuid4())
    body = _transfer_body(event_id, transferred_from=[str(uuid.uuid4())], transferred_to=[str(uuid.uuid4())])

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert response.status_code == 200
    event_row = await billing_db_pool.fetchrow(
        "SELECT * FROM revenuecat_webhook_events WHERE revenuecat_event_id = $1", event_id
    )
    assert event_row is not None
    assert event_row["app_user_id"] is None
    assert event_row["environment"] == "PRODUCTION"


async def test_transfer_without_environment_is_accepted_not_fabricated(client, billing_db_pool):
    event_id = str(uuid.uuid4())
    body = _transfer_body(
        event_id, transferred_from=[str(uuid.uuid4())], transferred_to=[str(uuid.uuid4())], environment=None,
    )

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert response.status_code == 200
    event_row = await billing_db_pool.fetchrow(
        "SELECT * FROM revenuecat_webhook_events WHERE revenuecat_event_id = $1", event_id
    )
    assert event_row is not None
    assert event_row["environment"] is None


async def test_transfer_missing_transferred_to_is_rejected(client):
    event_id = str(uuid.uuid4())
    body = _transfer_body(event_id, transferred_from=[str(uuid.uuid4())], transferred_to=[])

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert response.status_code == 400


async def test_transfer_missing_transferred_from_is_rejected(client):
    event_id = str(uuid.uuid4())
    body = _transfer_body(event_id, transferred_from=[], transferred_to=[str(uuid.uuid4())])

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert response.status_code == 400


async def test_lifecycle_event_missing_app_user_id_is_still_rejected(client):
    """Only TRANSFER gets the relaxed contract -- an ordinary lifecycle
    event omitting app_user_id is still a malformed delivery."""
    event_id = str(uuid.uuid4())
    payload = {
        "api_version": "1.0",
        "event": {
            "id": event_id,
            "type": "INITIAL_PURCHASE",
            "environment": "PRODUCTION",
            "event_timestamp_ms": int(time.time() * 1000),
        },
    }
    body = json.dumps(payload).encode("utf-8")

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert response.status_code == 400


async def test_invalid_authorization_rejected(client):
    event_id = str(uuid.uuid4())
    body = _event_body(event_id)
    headers = _headers(body)
    headers["Authorization"] = "wrong-value"

    response = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=headers)
    assert response.status_code == 401


async def test_missing_signature_rejected(client):
    event_id = str(uuid.uuid4())
    body = _event_body(event_id)

    response = await client.post(
        "/api/v2/webhooks/revenuecat", content=body, headers={"Authorization": AUTH_VALUE},
    )
    assert response.status_code == 401


async def test_duplicate_delivery_is_idempotent(client, billing_db_pool, app_db_pool):
    event_id = str(uuid.uuid4())
    body = _event_body(event_id)

    first = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))
    second = await client.post("/api/v2/webhooks/revenuecat", content=body, headers=_headers(body))

    assert first.status_code == 200
    assert second.status_code == 200

    event_count = await billing_db_pool.fetchval(
        "SELECT COUNT(*) FROM revenuecat_webhook_events WHERE revenuecat_event_id = $1", event_id
    )
    job_count = await app_db_pool.fetchval(
        "SELECT COUNT(*) FROM jobs WHERE job_type = 'revenuecat_webhook' AND request_id = $1", f"revenuecat:{event_id}"
    )
    assert event_count == 1
    assert job_count == 1


async def test_ten_concurrent_duplicate_deliveries_create_exactly_one_event_and_one_job(
    client, billing_db_pool, app_db_pool,
):
    event_id = str(uuid.uuid4())
    body = _event_body(event_id)
    headers = _headers(body)

    responses = await asyncio.gather(
        *[client.post("/api/v2/webhooks/revenuecat", content=body, headers=headers) for _ in range(10)]
    )

    assert all(r.status_code == 200 for r in responses)

    event_count = await billing_db_pool.fetchval(
        "SELECT COUNT(*) FROM revenuecat_webhook_events WHERE revenuecat_event_id = $1", event_id
    )
    job_count = await app_db_pool.fetchval(
        "SELECT COUNT(*) FROM jobs WHERE job_type = 'revenuecat_webhook' AND request_id = $1", f"revenuecat:{event_id}"
    )
    assert event_count == 1
    assert job_count == 1
