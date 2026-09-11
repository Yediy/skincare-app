"""app.domain.entitlement: EntitlementService/UsagePolicyService, the
RevenueCat-independent boundary business logic asks "what analysis
allowance does this user have?" through (Phase 8)."""
import uuid
from datetime import datetime, timezone

import pytest

from app.domain.entitlement import (
    AnalysisAlreadyCompletedError,
    AnalysisInProgressError,
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


async def test_usage_policy_service_reserved_replay_raises_in_progress_not_quota_exceeded(db_pool, app_db_pool):
    """Part II, Phase 14: a retried call with the same request_id
    while the original is still RESERVED (in-flight) must never be
    treated as a second request against an exhausted allowance -- but
    it also must not silently proceed to duplicate compute. It raises
    AnalysisInProgressError specifically, distinct from
    QuotaExceededError."""
    user_id = await _create_user(db_pool, "entitlement-replay@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=1))
    request_id = str(uuid.uuid4())

    first = await policy.reserve_analysis(user_id, request_id)

    with pytest.raises(AnalysisInProgressError) as exc_info:
        await policy.reserve_analysis(user_id, request_id)
    assert exc_info.value.reservation_id == first.id


async def test_usage_policy_service_consumed_replay_raises_already_completed(db_pool, app_db_pool):
    """A replay of a request_id whose analysis already ran to
    completion must never trigger a second computation (or a second
    quota charge) -- raises AnalysisAlreadyCompletedError."""
    user_id = await _create_user(db_pool, "entitlement-consumed-replay@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=3))
    request_id = str(uuid.uuid4())

    first = await policy.reserve_analysis(user_id, request_id)
    await policy.consume_reservation(user_id, first.id)

    with pytest.raises(AnalysisAlreadyCompletedError) as exc_info:
        await policy.reserve_analysis(user_id, request_id)
    assert exc_info.value.reservation_id == first.id


async def test_usage_policy_service_released_retry_succeeds_and_ends_consumed(db_pool, app_db_pool):
    """The full required lifecycle: RELEASED -> re-reserve -> success
    -> CONSUMED."""
    user_id = await _create_user(db_pool, "entitlement-released-retry@test.com")
    policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=2))
    request_id = str(uuid.uuid4())

    first = await policy.reserve_analysis(user_id, request_id)
    await policy.release_reservation(user_id, first.id)

    retry = await policy.reserve_analysis(user_id, request_id)
    assert retry.id == first.id
    assert retry.just_reactivated is True
    assert retry.attempt_count == 2

    await policy.consume_reservation(user_id, retry.id)
    row = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE id = $1", retry.id)
    assert row["status"] == "CONSUMED"


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
