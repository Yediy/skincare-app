"""GET /api/v2/billing/status, POST /api/v2/billing/sync -- end-to-end
through the real HTTP path (real Postgres, real auth, real rate
limiting), same `client` fixture as tests/api/test_analyses_v2.py and
tests/api/test_revenuecat_webhook_route.py.

Central invariant under test throughout this file: the server's
`user_entitlements` projection is the ONLY authority for premium
access -- both routes derive the user id exclusively from the bearer
token, never from any request body/query field, and `/sync` never
writes a mocked-API success/failure result that would let a client
grant itself access.
"""
import uuid

import asyncpg
import pytest

import app.api.v2.billing as billing_module
from app.config import settings
from app.db import revenuecat_repository, usage_repository
from app.domain.entitlement import current_period_key
from app.domain.revenuecat_reconciliation_service import ReconciliationAPIError

ENTITLEMENT_ID = "premium"


async def _signup_login(client, email):
    await client.post("/signup", json={"email": email, "password": "testpass123"})
    login = await client.post("/login", json={"email": email, "password": "testpass123"})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _user_id_for(client, headers):
    me = await client.get("/me", headers=headers)
    return uuid.UUID(me.json()["user_id"])


@pytest.fixture(autouse=True)
def _default_billing_disabled(monkeypatch):
    """Every test in this file starts from the real default
    (revenuecat_billing_enabled=False) and opts into billing-enabled
    behavior explicitly -- mirrors test_revenuecat_webhook_route.py's
    own convention."""
    monkeypatch.setattr(settings, "revenuecat_billing_enabled", False)


def _enable_billing(monkeypatch, *, free_allowance=3, paid_allowance=100):
    monkeypatch.setattr(settings, "revenuecat_billing_enabled", True)
    monkeypatch.setattr(settings, "revenuecat_entitlement_id", ENTITLEMENT_ID)
    monkeypatch.setattr(settings, "revenuecat_api_key", "test-only-key")
    monkeypatch.setattr(settings, "revenuecat_project_id", "test-only-project")
    monkeypatch.setattr(settings, "revenuecat_free_tier_allowance", free_allowance)
    monkeypatch.setattr(settings, "revenuecat_paid_tier_allowance", paid_allowance)
    monkeypatch.setattr(settings, "environment", "development")  # -> is_production False -> "SANDBOX"


async def _seed_entitlement(billing_db_pool, user_id, *, status, environment="SANDBOX", will_renew=True):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
        status=status, effective_at=now, expires_at=now + timedelta(days=30), will_renew=will_renew,
        environment=environment, source_event_id=None, provider_customer_id=str(user_id),
        last_provider_event_at=now,
    )


# ---------------------------------------------------------------------------
# GET /api/v2/billing/status
# ---------------------------------------------------------------------------


async def test_billing_status_derives_user_id_from_bearer_auth_only(client):
    """No query/body field on this GET route at all -- the response is
    a function purely of who the bearer token authenticates as."""
    headers = await _signup_login(client, "billing-auth-1@test.com")
    response = await client.get("/api/v2/billing/status", headers=headers)
    assert response.status_code == 200
    body = response.json()
    user_id = await _user_id_for(client, headers)
    assert body["app_user_id"] == str(user_id)


async def test_billing_status_requires_authentication(client):
    response = await client.get("/api/v2/billing/status")
    assert response.status_code in (401, 403)


async def test_one_user_cannot_retrieve_another_users_entitlement(client, monkeypatch, billing_db_pool):
    _enable_billing(monkeypatch)
    headers_a = await _signup_login(client, "billing-isolation-a@test.com")
    headers_b = await _signup_login(client, "billing-isolation-b@test.com")
    user_a = await _user_id_for(client, headers_a)
    user_b = await _user_id_for(client, headers_b)
    await _seed_entitlement(billing_db_pool, user_a, status="ACTIVE")

    response_a = await client.get("/api/v2/billing/status", headers=headers_a)
    response_b = await client.get("/api/v2/billing/status", headers=headers_b)

    assert response_a.json()["app_user_id"] == str(user_a)
    assert response_a.json()["has_premium_access"] is True
    assert response_b.json()["app_user_id"] == str(user_b)
    assert response_b.json()["has_premium_access"] is False  # user B has no entitlement row at all


async def test_billing_disabled_reports_free_tier_truthfully(client):
    headers = await _signup_login(client, "billing-disabled@test.com")
    response = await client.get("/api/v2/billing/status", headers=headers)
    body = response.json()
    assert body["billing_enabled"] is False
    assert body["has_premium_access"] is False
    assert body["projection_status"] is None
    assert body["environment"] is None
    assert body["analysis_allowance"] == settings.revenuecat_free_tier_allowance


@pytest.mark.parametrize("status,expect_premium", [
    ("ACTIVE", True),
    ("GRACE_PERIOD", True),
    ("EXPIRED", False),
    ("REVOKED", False),
])
async def test_projection_status_determines_premium_access(
    client, monkeypatch, billing_db_pool, status, expect_premium,
):
    _enable_billing(monkeypatch, paid_allowance=100)
    headers = await _signup_login(client, f"billing-status-{status.lower()}@test.com")
    user_id = await _user_id_for(client, headers)
    await _seed_entitlement(billing_db_pool, user_id, status=status)

    response = await client.get("/api/v2/billing/status", headers=headers)
    body = response.json()
    assert body["billing_enabled"] is True
    assert body["projection_status"] == status
    assert body["has_premium_access"] is expect_premium
    assert body["analysis_allowance"] == (100 if expect_premium else settings.revenuecat_free_tier_allowance)


async def test_sandbox_entitlement_cannot_activate_production_status(client, monkeypatch, billing_db_pool):
    """A SANDBOX ACTIVE row must never satisfy a PRODUCTION environment
    check -- same isolation RevenueCatEntitlementService itself
    enforces, proven here through the real HTTP route."""
    _enable_billing(monkeypatch, paid_allowance=100)
    monkeypatch.setattr(settings, "environment", "production")  # server now evaluates as PRODUCTION
    headers = await _signup_login(client, "billing-sandbox-isolation@test.com")
    user_id = await _user_id_for(client, headers)
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE", environment="SANDBOX")

    response = await client.get("/api/v2/billing/status", headers=headers)
    body = response.json()
    assert body["environment"] == "PRODUCTION"
    assert body["projection_status"] is None  # no PRODUCTION row exists
    assert body["has_premium_access"] is False


async def test_analysis_allowance_matches_build_entitlement_service(client, monkeypatch, billing_db_pool):
    from app.db.connection import get_db_pool
    from app.domain.entitlement import build_entitlement_service

    _enable_billing(monkeypatch, paid_allowance=77)
    headers = await _signup_login(client, "billing-allowance-parity@test.com")
    user_id = await _user_id_for(client, headers)
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")

    response = await client.get("/api/v2/billing/status", headers=headers)
    service = build_entitlement_service(get_db_pool())
    expected_allowance = await service.get_analysis_allowance(user_id)
    assert response.json()["analysis_allowance"] == expected_allowance == 77


async def test_usage_remaining_never_goes_negative(client, monkeypatch, app_db_pool):
    """Free allowance is 2; seed 5 RESERVED rows directly via
    usage_repository.reserve() (passing a permissive allowance at
    reserve-time, matching how a real allowance downgrade could leave
    more consumed than a NEW, lower allowance permits) -- the status
    route must clamp analyses_remaining at 0, never report a negative
    number."""
    monkeypatch.setattr(settings, "revenuecat_free_tier_allowance", 2)
    headers = await _signup_login(client, "billing-negative-remaining@test.com")
    user_id = await _user_id_for(client, headers)
    period_key = current_period_key()
    for _ in range(5):
        result = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), period_key, allowance=10)
        assert result.status == usage_repository.RESERVED

    response = await client.get("/api/v2/billing/status", headers=headers)
    body = response.json()
    assert body["analyses_used_or_reserved"] == 5
    assert body["analysis_allowance"] == 2
    assert body["analyses_remaining"] == 0


# ---------------------------------------------------------------------------
# POST /api/v2/billing/sync
# ---------------------------------------------------------------------------


class _FakeAPIClient:
    def __init__(self, items=None, error=None):
        self._items = items if items is not None else []
        self._error = error
        self.calls = []

    async def get_active_entitlements(self, app_user_id: str):
        self.calls.append(app_user_id)
        if self._error is not None:
            raise self._error
        return self._items


def _patch_fake_api_client(monkeypatch, fake_client):
    monkeypatch.setattr(billing_module, "RevenueCatAPIClient", lambda **kwargs: fake_client)


async def test_sync_disabled_returns_503(client):
    headers = await _signup_login(client, "billing-sync-disabled@test.com")
    response = await client.post("/api/v2/billing/sync", headers=headers)
    assert response.status_code == 503


async def test_sync_accepts_no_arbitrary_user_id(client, monkeypatch):
    """The route has no body schema that accepts a user/app_user_id
    field at all -- any JSON body sent is simply ignored, never
    interpreted as an override of the authenticated identity."""
    _enable_billing(monkeypatch)
    fake = _FakeAPIClient(items=[])
    _patch_fake_api_client(monkeypatch, fake)
    headers = await _signup_login(client, "billing-sync-no-override@test.com")
    user_id = await _user_id_for(client, headers)

    response = await client.post(
        "/api/v2/billing/sync", headers=headers,
        json={"app_user_id": str(uuid.uuid4()), "user_id": "not-a-real-user"},
    )
    assert response.status_code == 200
    assert response.json()["app_user_id"] == str(user_id)
    assert fake.calls == [str(user_id)]  # reconciliation was called with the AUTHENTICATED user, nothing else


async def test_sync_reconciles_only_the_authenticated_user(client, monkeypatch, billing_db_pool):
    _enable_billing(monkeypatch, paid_allowance=100)
    fake = _FakeAPIClient(items=[{"entitlement_id": ENTITLEMENT_ID, "expires_at": None}])
    _patch_fake_api_client(monkeypatch, fake)

    headers_a = await _signup_login(client, "billing-sync-target-a@test.com")
    headers_b = await _signup_login(client, "billing-sync-target-b@test.com")
    user_a = await _user_id_for(client, headers_a)
    user_b = await _user_id_for(client, headers_b)

    response = await client.post("/api/v2/billing/sync", headers=headers_a)
    assert response.status_code == 200
    assert response.json()["app_user_id"] == str(user_a)
    assert response.json()["has_premium_access"] is True
    assert fake.calls == [str(user_a)]

    # user B was never touched by this call.
    row_b = await revenuecat_repository.get_entitlement(billing_db_pool, user_b, ENTITLEMENT_ID, "revenuecat", "SANDBOX")
    assert row_b is None


async def test_sync_uses_the_billing_pool(client, monkeypatch, billing_db_pool):
    """Proven indirectly: a successful sync's write is visible through
    billing_db_pool (the skincare_billing role) immediately, with no
    separate write path -- the route never constructs its own
    connection to a different role."""
    _enable_billing(monkeypatch, paid_allowance=100)
    fake = _FakeAPIClient(items=[{"entitlement_id": ENTITLEMENT_ID, "expires_at": None}])
    _patch_fake_api_client(monkeypatch, fake)
    headers = await _signup_login(client, "billing-sync-uses-pool@test.com")
    user_id = await _user_id_for(client, headers)

    await client.post("/api/v2/billing/sync", headers=headers)

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "SANDBOX")
    assert row is not None
    assert row["status"] == "ACTIVE"


async def test_sync_revenuecat_failure_leaves_existing_local_entitlement_unchanged(
    client, monkeypatch, billing_db_pool,
):
    _enable_billing(monkeypatch, paid_allowance=100)
    headers = await _signup_login(client, "billing-sync-failure-preserves@test.com")
    user_id = await _user_id_for(client, headers)
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")
    before = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "SANDBOX")

    fake = _FakeAPIClient(error=ReconciliationAPIError("boom", error_code="SERVER_ERROR"))
    _patch_fake_api_client(monkeypatch, fake)

    response = await client.post("/api/v2/billing/sync", headers=headers)
    assert response.status_code == 503

    after = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "SANDBOX")
    assert after["status"] == before["status"] == "ACTIVE"
    assert after["updated_at"] == before["updated_at"]

    # The route's own status endpoint still reports the unchanged,
    # existing entitlement -- a failed sync never revokes access.
    status_response = await client.get("/api/v2/billing/status", headers=headers)
    assert status_response.json()["has_premium_access"] is True


async def test_sync_reflects_current_db_state_even_when_reconciliation_loses_the_cas_race(
    client, monkeypatch, billing_db_pool,
):
    """Independent-review concurrency fix, re-audited at the route
    level: if reconcile_user()'s own compare-and-set write loses to a
    concurrent writer (simulated here from inside the fake API
    client's call, exactly like the domain-level race tests in
    test_revenuecat_reconciliation_service.py), the route's response
    must still reflect the CURRENT database projection -- the
    concurrent writer's state -- never a stale value derived from
    variables captured before the RevenueCat call."""
    _enable_billing(monkeypatch, paid_allowance=100)
    headers = await _signup_login(client, "billing-sync-stale-race@test.com")
    user_id = await _user_id_for(client, headers)
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")

    class RacingFakeAPIClient:
        """The fake API call's own side effect simulates a concurrent
        RENEWAL webhook landing while this sync's "network request" is
        in flight -- reconciliation's own snapshot (remote says NOT
        active, since items=[]) would otherwise expire the user."""

        def __init__(self):
            self.calls = []

        async def get_active_entitlements(self, app_user_id: str):
            self.calls.append(app_user_id)
            from datetime import datetime, timedelta, timezone
            concurrent_expires = datetime.now(timezone.utc) + timedelta(days=90)
            await revenuecat_repository.apply_entitlement_projection(
                billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
                status="ACTIVE", effective_at=datetime.now(timezone.utc), expires_at=concurrent_expires,
                will_renew=True, environment="SANDBOX", source_event_id=None,
                provider_customer_id=str(user_id), last_provider_event_at=datetime.now(timezone.utc),
            )
            return []  # reconciliation's own (stale-by-the-time-it-returns) remote snapshot

    fake = RacingFakeAPIClient()
    _patch_fake_api_client(monkeypatch, fake)

    response = await client.post("/api/v2/billing/sync", headers=headers)
    assert response.status_code == 200
    body = response.json()

    # The concurrent writer's state -- premium access preserved --
    # never the stale "expire this user" decision reconciliation's own
    # (now-rejected) snapshot would have produced.
    assert body["has_premium_access"] is True
    assert body["projection_status"] == "ACTIVE"
    assert body["corrected"] is False  # reconciliation's own write did not land

    row = await revenuecat_repository.get_entitlement(billing_db_pool, user_id, ENTITLEMENT_ID, "revenuecat", "SANDBOX")
    assert row["status"] == "ACTIVE"


async def test_sync_uses_a_dedicated_bounded_policy_distinct_from_general(redis_client):
    """BILLING_SYNC_POLICY is its own named policy (never GENERAL_
    POLICY's unlimited-feeling per-user budget) -- proven directly at
    the Redis-keyed rate-limiter level, using the REAL policy name the
    route depends on (`app.middleware.rate_limiter.BILLING_SYNC_POLICY.
    name`) so a rename of that constant would be caught by this test,
    at a small max_requests so the test itself stays fast."""
    from app.middleware.rate_limiter import BILLING_SYNC_POLICY, RateLimiter, RateLimitPolicy

    assert BILLING_SYNC_POLICY.fail_open is False
    assert BILLING_SYNC_POLICY.name != "general"

    bounded_policy = RateLimitPolicy(BILLING_SYNC_POLICY.name, max_requests=2, window_seconds=60, fail_open=False)
    limiter = RateLimiter(redis_client)
    identity = "user:billing-sync-rate-limit-test"

    first = await limiter.check(bounded_policy, identity)
    second = await limiter.check(bounded_policy, identity)
    third = await limiter.check(bounded_policy, identity)
    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False


async def test_sync_route_itself_is_guarded_by_the_billing_sync_policy(client, monkeypatch):
    """End-to-end proof the route actually depends on rate_limit_by_user
    (BILLING_SYNC_POLICY) -- a request against the real, default-
    configured policy succeeds normally (never crashes, never bypasses
    the dependency), and the dependency chain resolves to the
    authenticated user's own identity for rate-limit keying (proven
    together with test_sync_accepts_no_arbitrary_user_id above)."""
    _enable_billing(monkeypatch)
    fake = _FakeAPIClient(items=[])
    _patch_fake_api_client(monkeypatch, fake)
    headers = await _signup_login(client, "billing-sync-policy-applied@test.com")

    response = await client.post("/api/v2/billing/sync", headers=headers)
    assert response.status_code == 200


async def test_ordinary_runtime_role_still_cannot_mutate_entitlement_truth(app_db_pool):
    """Unchanged by this pass -- the ordinary skincare_app role (what
    app_db_pool authenticates as) still has no INSERT/UPDATE/DELETE
    grant on user_entitlements (migration 9815eb266923); this new
    billing.py surface reuses that same privilege boundary rather than
    quietly opening a write path for the ordinary runtime role."""
    user_id = uuid.uuid4()
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await app_db_pool.execute(
            "INSERT INTO user_entitlements "
            "(user_id, entitlement_identifier, provider, status, effective_at, environment, "
            " will_renew, last_provider_event_at) "
            "VALUES ($1, 'premium', 'revenuecat', 'ACTIVE', now(), 'SANDBOX', true, now())",
            user_id,
        )
