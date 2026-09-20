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
# RevenueCat's own internal entitlement resource ID -- a distinct value
# from ENTITLEMENT_ID (the lookup key) used ONLY in the fake remote
# API responses below (matching the real active_entitlements shape),
# never for local reads/writes.
ENTITLEMENT_RESOURCE_ID = "entl_test_premium_123"
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


def _active_entitlement_item(expires_at: datetime, *, entitlement_id: str = ENTITLEMENT_RESOURCE_ID) -> dict:
    """Real documented shape: entitlement_id + expires_at (ms) --
    never gives_access/auto_renewal_status, which the real endpoint
    does not document. `entitlement_id` here is RevenueCat's own
    internal resource ID on a real response, so it defaults to
    ENTITLEMENT_RESOURCE_ID, never the lookup key."""
    return {
        "object": "customer.active_entitlement",
        "entitlement_id": entitlement_id,
        "expires_at": int(expires_at.timestamp() * 1000),
    }


def _service(pool, api_client, *, entitlement_resource_id=ENTITLEMENT_RESOURCE_ID):
    return RevenueCatReconciliationService(
        pool, api_client, entitlement_identifier=ENTITLEMENT_ID,
        entitlement_resource_id=entitlement_resource_id, environment="PRODUCTION",
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


async def test_real_v2_resource_id_shape_activates_local_lookup_key_projection(db_pool, billing_db_pool):
    """Regression for the entitlement-id/resource-id conflation bug:
    a real documented v2 response (`entitlement_id` = RevenueCat's
    internal resource ID, e.g. "entl_test_premium_123") must activate
    the local projection, which stays keyed by the lookup key
    ("premium"), never by the resource ID itself."""
    user_id = await _create_user(db_pool, "recon-real-v2-shape@test.com")
    expires = T0 + timedelta(days=30)
    api_client = FakeAPIClient(
        items=[_active_entitlement_item(expires, entitlement_id=ENTITLEMENT_RESOURCE_ID)]
    )

    outcome = await _service(
        billing_db_pool, api_client, entitlement_resource_id=ENTITLEMENT_RESOURCE_ID,
    ).reconcile_user(user_id)

    assert outcome.remote_active is True
    assert outcome.corrected is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row is not None  # local row is keyed by the lookup key, "premium"
    assert row["status"] == "ACTIVE"


async def test_lookup_key_in_remote_entitlement_id_field_is_not_accepted(db_pool, billing_db_pool):
    """Guards against the fake HTTP-contract fixture creeping back in:
    a remote item whose `entitlement_id` is the lookup key itself
    ("premium") -- never a real shape RevenueCat's v2 API actually
    returns -- must NOT match the configured resource ID, even though
    it's the correct entitlement's identifier under the OTHER
    (lookup-key) identity system."""
    user_id = await _create_user(db_pool, "recon-lookup-key-not-accepted@test.com")
    api_client = FakeAPIClient(
        items=[_active_entitlement_item(T0 + timedelta(days=30), entitlement_id=ENTITLEMENT_ID)]
    )

    outcome = await _service(
        billing_db_pool, api_client, entitlement_resource_id=ENTITLEMENT_RESOURCE_ID,
    ).reconcile_user(user_id)

    assert outcome.remote_active is False

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row is None  # no access was granted


async def test_wrong_internal_entitlement_resource_id_does_not_grant_access(db_pool, billing_db_pool):
    """A remote active entitlement for an unrelated RevenueCat product
    (a different internal resource ID) must not activate local
    "premium" access, even though some entitlement is active for this
    customer."""
    user_id = await _create_user(db_pool, "recon-wrong-resource-id@test.com")
    api_client = FakeAPIClient(
        items=[_active_entitlement_item(T0 + timedelta(days=30), entitlement_id="entl_other_product")]
    )

    outcome = await _service(
        billing_db_pool, api_client, entitlement_resource_id=ENTITLEMENT_RESOURCE_ID,
    ).reconcile_user(user_id)

    assert outcome.remote_active is False

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row is None


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


# ---------------------------------------------------------------------------
# Concurrency / compare-and-set race regressions (independent-review fix).
#
# Each `RacingAPIClient` below performs a CONCURRENT mutation of the
# same row from INSIDE `get_active_entitlements()` -- i.e. exactly at
# the point in `reconcile_user()` representing "the network call is
# in flight" -- deterministically, with no sleep/timing involved. This
# reproduces the exact failure sequence the review described: the
# concurrent writer's change happens strictly between reconciliation's
# own local read (which captured a `row_version` before this call) and
# reconciliation's own write (which is attempted only after this call
# returns).
# ---------------------------------------------------------------------------


class RacingAPIClient:
    """Wraps a real write (`side_effect`, typically a direct
    `apply_entitlement_projection()` call simulating a webhook, or a
    nested `reconcile_user()` call simulating a second concurrent
    reconciliation attempt) that runs BEFORE this fake client returns
    its own (possibly stale-relative-to-that-write) response."""

    def __init__(self, items, side_effect):
        self._items = items
        self._side_effect = side_effect
        self.calls = 0

    async def get_active_entitlements(self, app_user_id: str):
        self.calls += 1
        await self._side_effect()
        return self._items


async def test_case1_active_snapshot_cannot_overwrite_newer_expiration_webhook(db_pool, billing_db_pool):
    """Local starts EXPIRED. Reconciliation's remote check says ACTIVE
    (a mismatch it intends to correct). While its network call is "in
    flight", a real EXPIRATION-confirming webhook re-applies EXPIRED
    with a fresh timestamp (bumping the row's version). Reconciliation
    must not restore ACTIVE over that newer webhook state."""
    user_id = await _create_user(db_pool, "recon-case1@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="EXPIRED")

    async def concurrent_expiration_webhook():
        await revenuecat_repository.apply_entitlement_projection(
            billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
            status="EXPIRED", effective_at=T0 + timedelta(minutes=5), expires_at=T0 + timedelta(minutes=5),
            will_renew=False, environment="PRODUCTION", source_event_id=None,
            provider_customer_id=str(user_id), last_provider_event_at=T0 + timedelta(minutes=5),
        )

    api_client = RacingAPIClient(
        items=[_active_entitlement_item(T0 + timedelta(days=30))], side_effect=concurrent_expiration_webhook,
    )

    outcome = await _service(billing_db_pool, api_client).reconcile_user(user_id)

    assert outcome.mismatch_found is True
    assert outcome.corrected is False
    assert outcome.stale_snapshot is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "EXPIRED"  # the webhook's state, never restored to ACTIVE


async def test_case2_expired_snapshot_cannot_overwrite_newer_purchase_or_renewal(db_pool, billing_db_pool):
    """Local starts ACTIVE. Reconciliation's remote check says NOT
    active (a mismatch it intends to correct to EXPIRED). While its
    network call is "in flight", a real RENEWAL webhook re-confirms
    ACTIVE with a fresh timestamp and a new expiration. Reconciliation
    must not expire that newer, paid state."""
    user_id = await _create_user(db_pool, "recon-case2@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")
    renewed_expires = T0 + timedelta(days=60)

    async def concurrent_renewal_webhook():
        await revenuecat_repository.apply_entitlement_projection(
            billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
            status="ACTIVE", effective_at=T0 + timedelta(minutes=5), expires_at=renewed_expires,
            will_renew=True, environment="PRODUCTION", source_event_id=None,
            provider_customer_id=str(user_id), last_provider_event_at=T0 + timedelta(minutes=5),
        )

    api_client = RacingAPIClient(items=[], side_effect=concurrent_renewal_webhook)

    outcome = await _service(billing_db_pool, api_client).reconcile_user(user_id)

    assert outcome.mismatch_found is True
    assert outcome.corrected is False
    assert outcome.stale_snapshot is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "ACTIVE"  # the renewal's state, never expired by the stale reconciliation
    assert row["expires_at"] == renewed_expires


async def test_case3_no_local_row_race_cannot_overwrite_concurrently_created_row(db_pool, billing_db_pool):
    """Reconciliation observes NO local entitlement row. While its
    network call is "in flight" (remote says ACTIVE), a webhook
    creates the row as EXPIRED (e.g. an immediate refund/revocation).
    Reconciliation must not overwrite that newly-created row."""
    user_id = await _create_user(db_pool, "recon-case3@test.com")

    async def concurrent_webhook_creates_row():
        await revenuecat_repository.apply_entitlement_projection(
            billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
            status="EXPIRED", effective_at=T0, expires_at=T0, will_renew=False,
            environment="PRODUCTION", source_event_id=None,
            provider_customer_id=str(user_id), last_provider_event_at=T0,
        )

    api_client = RacingAPIClient(
        items=[_active_entitlement_item(T0 + timedelta(days=30))], side_effect=concurrent_webhook_creates_row,
    )

    outcome = await _service(billing_db_pool, api_client).reconcile_user(user_id)

    assert outcome.mismatch_found is True
    assert outcome.corrected is False
    assert outcome.stale_snapshot is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row is not None
    assert row["status"] == "EXPIRED"  # the webhook's newly-created row, never overwritten to ACTIVE


async def test_case6_concurrent_reconciliation_attempts_do_not_clobber_each_other(db_pool, billing_db_pool):
    """Two reconciliation attempts for the same user race. The
    "inner" one fully completes (read, remote call, CAS write) from
    INSIDE the "outer" one's own network call, so it wins the write
    race despite starting after the outer one's own local read. The
    outer attempt's own write, based on its now-stale read, must lose
    the compare-and-set rather than clobber the inner attempt's
    result -- proven by using a distinguishable `expires_at` per
    attempt, so a silent last-write-wins bug would be directly
    observable in the final row."""
    user_id = await _create_user(db_pool, "recon-case6@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="EXPIRED")

    inner_expires = T0 + timedelta(days=99)
    outer_expires = T0 + timedelta(days=10)
    inner_outcome_holder: dict = {}

    async def run_inner_reconciliation():
        inner_client = FakeAPIClient(items=[_active_entitlement_item(inner_expires)])
        inner_service = _service(billing_db_pool, inner_client)
        inner_outcome_holder["outcome"] = await inner_service.reconcile_user(user_id)

    outer_api_client = RacingAPIClient(
        items=[_active_entitlement_item(outer_expires)], side_effect=run_inner_reconciliation,
    )

    outer_outcome = await _service(billing_db_pool, outer_api_client).reconcile_user(user_id)
    inner_outcome = inner_outcome_holder["outcome"]

    assert inner_outcome.corrected is True
    assert outer_outcome.corrected is False
    assert outer_outcome.stale_snapshot is True

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "PRODUCTION")
    assert row["status"] == "ACTIVE"
    assert row["expires_at"] == inner_expires  # the attempt that actually won the write race
    assert row["expires_at"] != outer_expires  # the outer attempt's stale write never landed
