"""app.domain.revenuecat_reconciliation_service -- correcting the
local projection against a (faked, never real) RevenueCat API
response. FakeAPIClient below is a plain duck-typed stand-in
(get_active_entitlements(app_user_id) -> list[dict]), never a real
httpx call -- this suite has no network access to RevenueCat and
shouldn't need any. See test_revenuecat_reconciliation_http_contract.py
for the real RevenueCatAPIClient tested against httpx.MockTransport
(the actual HTTP contract, not this duck-typed stand-in).

Writes go through billing_db_pool (the skincare_billing role) --
reconciliation corrections are a privileged billing-mutation path,
same as webhook-driven projection, as of migration 9815eb266923.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import revenuecat_repository
from app.domain.revenuecat_reconciliation_service import (
    ReconciliationAPIError,
    RevenueCatReconciliationService,
)

ENTITLEMENT_ID = "premium"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def _seed_entitlement(billing_db_pool, user_id, *, status, will_renew=True):
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
        status=status, effective_at=T0, expires_at=T0 + timedelta(days=30), will_renew=will_renew,
        environment="PRODUCTION", source_event_id=None, provider_customer_id=str(user_id),
        last_provider_event_at=T0,
    )


class FakeAPIClient:
    def __init__(self, items=None, error=None):
        self._items = items if items is not None else []
        self._error = error
        self.calls = []

    async def get_active_entitlements(self, app_user_id: str):
        self.calls.append(app_user_id)
        if self._error is not None:
            raise self._error
        return self._items


def _active_entitlement_item(expires_at: datetime) -> dict:
    """Real documented shape: entitlement_id + expires_at (ms) --
    never gives_access/auto_renewal_status, which the real endpoint
    does not document."""
    return {
        "object": "customer.active_entitlement",
        "entitlement_id": ENTITLEMENT_ID,
        "expires_at": int(expires_at.timestamp() * 1000),
    }


def _service(pool, api_client):
    return RevenueCatReconciliationService(
        pool, api_client, entitlement_identifier=ENTITLEMENT_ID, environment="PRODUCTION",
    )


async def test_reconciliation_detects_no_mismatch_when_states_agree(db_pool, billing_db_pool):
    user_id = await _create_user(db_pool, "recon-agree@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")
    api_client = FakeAPIClient(items=[_active_entitlement_item(T0 + timedelta(days=30))])

    outcome = await _service(billing_db_pool, api_client).reconcile_user(user_id)

    assert outcome.mismatch_found is False
    assert outcome.corrected is False


async def test_reconciliation_detects_and_repairs_local_stale_active(db_pool, billing_db_pool):
    """Local says ACTIVE (e.g. a missed EXPIRATION webhook), remote
    says no active entitlement -- reconciliation must correct local to
    reflect the true, no-longer-entitled state."""
    user_id = await _create_user(db_pool, "recon-stale-local@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")
    api_client = FakeAPIClient(items=[])

    outcome = await _service(billing_db_pool, api_client).reconcile_user(user_id)

    assert outcome.mismatch_found is True
    assert outcome.corrected is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "EXPIRED"


async def test_reconciliation_detects_and_repairs_missed_purchase(db_pool, billing_db_pool):
    """Local has no entitlement row at all (e.g. a missed
    INITIAL_PURCHASE webhook), remote says active -- reconciliation
    must grant access locally."""
    user_id = await _create_user(db_pool, "recon-missed-purchase@test.com")
    expires = T0 + timedelta(days=30)
    api_client = FakeAPIClient(items=[_active_entitlement_item(expires)])

    outcome = await _service(billing_db_pool, api_client).reconcile_user(user_id)

    assert outcome.mismatch_found is True
    assert outcome.corrected is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "ACTIVE"
    assert row["expires_at"] == expires


async def test_reconciliation_preserves_existing_will_renew_never_fabricates_true(db_pool, billing_db_pool):
    """The active-entitlements response has no renewal-state field --
    a correction from EXPIRED back to ACTIVE must preserve whatever
    will_renew was already locally known (here: False, from a prior
    normal cancellation), never silently reset it to True."""
    user_id = await _create_user(db_pool, "recon-preserve-will-renew@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="EXPIRED", will_renew=False)
    api_client = FakeAPIClient(items=[_active_entitlement_item(T0 + timedelta(days=30))])

    await _service(billing_db_pool, api_client).reconcile_user(user_id)

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "ACTIVE"
    assert row["will_renew"] is False


async def test_reconciliation_api_failure_raises_and_does_not_touch_local_state(db_pool, billing_db_pool):
    user_id = await _create_user(db_pool, "recon-api-failure@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")
    api_client = FakeAPIClient(error=ReconciliationAPIError("simulated outage", error_code="SERVER_ERROR"))

    with pytest.raises(ReconciliationAPIError):
        await _service(billing_db_pool, api_client).reconcile_user(user_id)

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "ACTIVE"  # unchanged


async def test_reconcile_batch_continues_past_one_users_api_failure(db_pool, billing_db_pool):
    good_user = await _create_user(db_pool, "recon-batch-good@test.com")
    bad_user = await _create_user(db_pool, "recon-batch-bad@test.com")
    await _seed_entitlement(billing_db_pool, good_user, status="ACTIVE")

    class MixedClient:
        async def get_active_entitlements(self, app_user_id: str):
            if app_user_id == str(bad_user):
                raise ReconciliationAPIError("simulated per-user failure", error_code="SERVER_ERROR")
            return [_active_entitlement_item(T0 + timedelta(days=30))]

    outcomes = await _service(billing_db_pool, MixedClient()).reconcile_batch([bad_user, good_user], delay_seconds=0)

    assert len(outcomes) == 1
    assert outcomes[0].user_id == good_user
