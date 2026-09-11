"""RevenueCatEntitlementService: reads ONLY the local Postgres
projection -- see app/domain/entitlement.py's module docstring for why
that's the whole point (Section 14 of the billing brief: a RevenueCat
outage must never block an access decision this app can already answer
locally)."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db import revenuecat_repository
from app.domain.entitlement import RevenueCatEntitlementService, build_entitlement_service, FreeTierEntitlementService

ENTITLEMENT_ID = "premium"
FREE_ALLOWANCE = 3
PAID_ALLOWANCE = 100
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


def _service(pool, environment="PRODUCTION"):
    return RevenueCatEntitlementService(
        pool, entitlement_identifier=ENTITLEMENT_ID, environment=environment,
        free_allowance=FREE_ALLOWANCE, paid_allowance=PAID_ALLOWANCE,
    )


async def _seed_entitlement(billing_db_pool, user_id, *, status, environment="PRODUCTION"):
    """Seeds through billing_db_pool (skincare_billing) -- the ordinary
    skincare_app role (what `_service()` below reads through, proving
    the actual production read path) has no write grant on
    user_entitlements at all as of migration 9815eb266923."""
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_id, entitlement_identifier=ENTITLEMENT_ID, provider="revenuecat",
        status=status, effective_at=T0, expires_at=T0 + timedelta(days=30), will_renew=True,
        environment=environment, source_event_id=None, provider_customer_id=str(user_id),
        last_provider_event_at=T0,
    )


async def test_user_with_no_entitlement_row_gets_free_allowance(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "rces-free@test.com")
    allowance = await _service(app_db_pool).get_analysis_allowance(user_id)
    assert allowance == FREE_ALLOWANCE


async def test_active_entitlement_gets_paid_allowance(db_pool, app_db_pool, billing_db_pool):
    user_id = await _create_user(db_pool, "rces-active@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")
    allowance = await _service(app_db_pool).get_analysis_allowance(user_id)
    assert allowance == PAID_ALLOWANCE


async def test_grace_period_entitlement_gets_paid_allowance(db_pool, app_db_pool, billing_db_pool):
    user_id = await _create_user(db_pool, "rces-grace@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="GRACE_PERIOD")
    allowance = await _service(app_db_pool).get_analysis_allowance(user_id)
    assert allowance == PAID_ALLOWANCE


@pytest.mark.parametrize("status", ["EXPIRED", "REVOKED"])
async def test_expired_or_revoked_entitlement_gets_free_allowance(db_pool, app_db_pool, billing_db_pool, status):
    user_id = await _create_user(db_pool, f"rces-{status.lower()}@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status=status)
    allowance = await _service(app_db_pool).get_analysis_allowance(user_id)
    assert allowance == FREE_ALLOWANCE


async def test_sandbox_entitlement_never_grants_production_allowance(db_pool, app_db_pool, billing_db_pool):
    user_id = await _create_user(db_pool, "rces-sandbox@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE", environment="SANDBOX")

    production_service = _service(app_db_pool, environment="PRODUCTION")
    assert await production_service.get_analysis_allowance(user_id) == FREE_ALLOWANCE

    sandbox_service = _service(app_db_pool, environment="SANDBOX")
    assert await sandbox_service.get_analysis_allowance(user_id) == PAID_ALLOWANCE


async def test_entitlement_service_never_makes_a_network_call(db_pool, app_db_pool, billing_db_pool, monkeypatch):
    """Proves Section 14 directly: even if RevenueCat's API is
    completely unreachable, the access decision (a local SELECT) must
    still succeed -- simulated here by making any httpx client
    construction raise, so the test fails loudly if
    RevenueCatEntitlementService ever attempts one."""
    def _explode(*args, **kwargs):
        raise AssertionError("RevenueCatEntitlementService must never construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _explode)

    user_id = await _create_user(db_pool, "rces-outage@test.com")
    await _seed_entitlement(billing_db_pool, user_id, status="ACTIVE")

    allowance = await _service(app_db_pool).get_analysis_allowance(user_id)
    assert allowance == PAID_ALLOWANCE


async def test_build_entitlement_service_defaults_to_free_tier_when_billing_disabled(app_db_pool, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "revenuecat_billing_enabled", False)
    service = build_entitlement_service(app_db_pool)
    assert isinstance(service, FreeTierEntitlementService)


async def test_build_entitlement_service_returns_revenuecat_service_when_enabled(app_db_pool, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "revenuecat_billing_enabled", True)
    service = build_entitlement_service(app_db_pool)
    assert isinstance(service, RevenueCatEntitlementService)
