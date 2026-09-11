"""Entitlement/usage-policy boundary (Phase 8 of the product/usage
foundation pass). RevenueCat is deliberately NOT integrated this pass
-- this module exists so business logic can ask "what analysis
allowance does this user have?" through EntitlementService, and "may
this specific request proceed, and how do I account for it?" through
UsagePolicyService, without either question depending on RevenueCat or
any other specific billing provider. When RevenueCat is integrated
later, it becomes a new EntitlementService implementation behind this
same interface -- the domain layer (perform_analysis, /analyze) does
not change.

FreeTierEntitlementService below is a real, working, deliberately
simple implementation (a fixed monthly allowance), not a mock -- it is
what every environment (including production) uses until a real
billing-aware EntitlementService replaces it. It is not a placeholder
that raises NotImplementedError.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from app.db import usage_repository
from app.observability import events as observability_events


class EntitlementService(ABC):
    """What allowance does this user have, this period? The only
    question this interface answers -- it says nothing about whether
    any particular request may proceed (that composition, plus
    idempotency/reservation accounting, is UsagePolicyService's job,
    built on top of this)."""

    @abstractmethod
    async def get_analysis_allowance(self, user_id: UUID) -> int:
        """Number of analyses this user may reserve per period.
        period_key's own granularity (see UsagePolicyService) defines
        what "period" means -- currently always calendar-month."""
        raise NotImplementedError


class FreeTierEntitlementService(EntitlementService):
    """Fixed free-tier allowance, the same for every user -- real,
    used by every environment today, not a test-only stub. Swapping in
    a billing-aware EntitlementService later (e.g. one that reads a
    RevenueCat-synced entitlement record) requires no change to any
    caller of this interface."""

    def __init__(self, monthly_allowance: int = 3):
        self._monthly_allowance = monthly_allowance

    async def get_analysis_allowance(self, user_id: UUID) -> int:
        return self._monthly_allowance


class RevenueCatEntitlementService(EntitlementService):
    """Reads the LOCAL Postgres entitlement projection
    (user_entitlements, populated by
    app/domain/revenuecat_entitlement_processor.py) -- never calls the
    RevenueCat API. This is what makes Section 14 of the billing brief
    true: a RevenueCat outage affects reconciliation freshness (see
    app/domain/revenuecat_reconciliation_service.py), never whether
    POST /api/v2/analyses or the worker can decide a user's allowance,
    since that decision is one local SELECT.

    Filters by `environment` explicitly (Section 15): a SANDBOX
    purchase can never satisfy a PRODUCTION allowance check, regardless
    of what environment this process happens to be running in.

    Allowance is a plain free/paid split (Section 12) -- not a
    hardcoded number in this class, both values are configuration
    (settings.revenuecat_free_tier_allowance/paid_tier_allowance),
    constructor-injected so a test can exercise any pair of values
    without touching global settings.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        entitlement_identifier: str,
        environment: str,
        free_allowance: int,
        paid_allowance: int,
    ):
        self._pool = pool
        self._entitlement_identifier = entitlement_identifier
        self._environment = environment
        self._free_allowance = free_allowance
        self._paid_allowance = paid_allowance

    async def get_analysis_allowance(self, user_id: UUID) -> int:
        from app.db import revenuecat_repository

        row = await revenuecat_repository.get_entitlement(
            self._pool, user_id, self._entitlement_identifier, "revenuecat", self._environment,
        )
        if row is not None and row["status"] in ("ACTIVE", "GRACE_PERIOD"):
            return self._paid_allowance
        return self._free_allowance


def build_entitlement_service(pool: asyncpg.Pool) -> EntitlementService:
    """Composition-root factory (used by app/main.py, app/api/v2/
    analyses.py, app/workers/analysis_worker.py): every real caller
    asks for "the current EntitlementService" through this one
    function rather than constructing FreeTierEntitlementService or
    RevenueCatEntitlementService directly, so flipping
    settings.revenuecat_billing_enabled is the only change needed to
    switch every quota decision in the app -- no call site touches
    perform_analysis/AnalysisSubmissionService/AnalysisExecutionService
    to do it (see BILLING_ARCHITECTURE.md's "what this pass does not
    touch" section). Defaults to FreeTierEntitlementService, so an
    unconfigured deployment's behavior is byte-for-byte unchanged from
    before this pass."""
    from app.config import settings

    if not settings.revenuecat_billing_enabled:
        return FreeTierEntitlementService(monthly_allowance=settings.revenuecat_free_tier_allowance)
    return RevenueCatEntitlementService(
        pool,
        entitlement_identifier=settings.revenuecat_entitlement_id,
        environment="PRODUCTION" if settings.is_production else "SANDBOX",
        free_allowance=settings.revenuecat_free_tier_allowance,
        paid_allowance=settings.revenuecat_paid_tier_allowance,
    )


def current_period_key(now: Optional[datetime] = None) -> str:
    """Calendar-month period, UTC: "2026-09". The only period
    granularity this pass implements -- documented, not hidden, since
    a future entitlement model might need a different one (rolling
    30-day, weekly, etc.), which would be a UsagePolicyService change,
    not an analysis_usage schema change (period_key is just a string)."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


class QuotaExceededError(Exception):
    """Raised by UsagePolicyService.reserve_analysis() when the user's
    allowance for the current period is already exhausted. A plain
    domain exception, framework-agnostic like
    ConsentRequiredError/InvalidImageError in
    app/domain/analysis_service.py -- the HTTP layer maps it to 429."""


class AnalysisAlreadyCompletedError(Exception):
    """Raised when request_id replays a reservation that already
    reached CONSUMED -- the analysis already ran to completion once.
    Part II, Phase 14's "never compute again if persisted result
    exists": this pass does not yet have an analysis_results table
    (Part III) to fetch and return the old result from, so the honest,
    correctly-scoped behavior today is to refuse to recompute rather
    than silently doing the (expensive, quota-double-charging) work
    again -- the HTTP layer maps this to 409. Once analysis_results
    exists, the caller catching this can be upgraded to actually
    return the persisted result instead of raising to the client."""

    def __init__(self, reservation_id: UUID):
        self.reservation_id = reservation_id
        super().__init__(f"request_id already completed (reservation {reservation_id})")


class AnalysisInProgressError(Exception):
    """Raised when request_id replays a reservation still RESERVED by
    a *different* attempt (a concurrent caller, or an earlier attempt
    that hasn't reached consume()/release() yet) -- as opposed to a
    RELEASED reservation this exact call just legitimately
    re-activated (see UsageReservation.replay/just_reactivated). Do
    not create duplicate compute for the same in-flight request_id --
    the HTTP layer maps this to 409."""

    def __init__(self, reservation_id: UUID):
        self.reservation_id = reservation_id
        super().__init__(f"request_id already in progress (reservation {reservation_id})")


@dataclass(frozen=True)
class UsageReservation:
    id: Optional[UUID]
    replay: bool  # True if this is an idempotent replay of an existing request_id, not a fresh reservation
    just_reactivated: bool = False  # True only if this call performed a RELEASED -> RESERVED transition
    attempt_count: int = 1


class UsagePolicyService:
    """Composes EntitlementService (how much) with
    app/db/usage_repository.py (the atomic reserve/consume/release
    primitives) into the actual request lifecycle: idempotency check
    -> reserve -> (caller does the expensive work) -> consume or
    release."""

    def __init__(self, pool: asyncpg.Pool, entitlement_service: EntitlementService):
        self._pool = pool
        self._entitlement_service = entitlement_service

    async def reserve_analysis(self, user_id: UUID, request_id: str) -> UsageReservation:
        allowance = await self._entitlement_service.get_analysis_allowance(user_id)
        period_key = current_period_key()
        result = await usage_repository.reserve(self._pool, user_id, request_id, period_key, allowance)

        if result.status == usage_repository.DENIED:
            observability_events.quota_denial(user_id=str(user_id))
            raise QuotaExceededError(
                f"Analysis allowance ({allowance}/period) exhausted for the current period."
            )
        if result.status == usage_repository.CONSUMED:
            raise AnalysisAlreadyCompletedError(result.id)
        if result.status == usage_repository.RESERVED and result.replay and not result.just_reactivated:
            raise AnalysisInProgressError(result.id)

        # Either a genuinely fresh reservation, or a legitimate
        # RELEASED -> RESERVED reactivation (just_reactivated=True) --
        # both are real go-ahead-and-compute outcomes.
        return UsageReservation(
            id=result.id, replay=result.replay,
            just_reactivated=result.just_reactivated, attempt_count=result.attempt_count,
        )

    async def consume_reservation(self, user_id: UUID, reservation_id: UUID) -> None:
        await usage_repository.consume(self._pool, user_id, reservation_id)

    async def release_reservation(self, user_id: UUID, reservation_id: UUID) -> None:
        await usage_repository.release(self._pool, user_id, reservation_id)
