"""app.domain.entitlement: EntitlementService/UsagePolicyService, the
RevenueCat-independent boundary business logic asks "what analysis
allowance does this user have?" through (Phase 8)."""
import uuid
from datetime import datetime, timezone

import pytest

from app.domain.entitlement import (
    FreeTierEntitlementService,
    QuotaExceededError,
    UsagePolicyService,
    current_period_key,
)


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


def test_current_period_key_is_calendar_month_utc():
    moment = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert current_period_key(moment) == "2026-09"


async def test_free_tier_entitlement_service_returns_configured_allowance():
    service = FreeTierEntitlementService(monthly_allowance=5)
    assert await service.get_analysis_allowance(uuid.uuid4()) == 5


async def test_free_tier_entitlement_service_default_allowance():
    service = FreeTierEntitlementService()
    assert await service.get_analysis_allowance(uuid.uuid4()) == 3


async def test_usage_policy_service_allows_reservation_within_allowance(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "entitlement-allow@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=2))

    reservation = await policy.reserve_analysis(user_id, str(uuid.uuid4()))
    assert reservation.id is not None
    assert reservation.replay is False


async def test_usage_policy_service_raises_quota_exceeded_once_allowance_used(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "entitlement-deny@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=1))

    await policy.reserve_analysis(user_id, str(uuid.uuid4()))

    with pytest.raises(QuotaExceededError):
        await policy.reserve_analysis(user_id, str(uuid.uuid4()))


async def test_usage_policy_service_replay_does_not_raise_quota_exceeded(db_pool, app_db_pool):
    """A retried call with the same request_id must return the
    existing reservation, not be treated as a second request against
    an exhausted allowance."""
    user_id = await _create_user(db_pool, "entitlement-replay@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=1))
    request_id = str(uuid.uuid4())

    first = await policy.reserve_analysis(user_id, request_id)
    second = await policy.reserve_analysis(user_id, request_id)

    assert first.id == second.id
    assert second.replay is True


async def test_usage_policy_service_consume_and_release(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "entitlement-lifecycle@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=2))

    reservation = await policy.reserve_analysis(user_id, str(uuid.uuid4()))
    await policy.consume_reservation(user_id, reservation.id)

    row = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE id = $1", reservation.id)
    assert row["status"] == "CONSUMED"

    reservation2 = await policy.reserve_analysis(user_id, str(uuid.uuid4()))
    await policy.release_reservation(user_id, reservation2.id)
    row2 = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE id = $1", reservation2.id)
    assert row2["status"] == "RELEASED"
