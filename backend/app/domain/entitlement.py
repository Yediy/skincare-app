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


@dataclass(frozen=True)
class UsageReservation:
    id: Optional[UUID]
    replay: bool  # True if this is an idempotent replay of an existing request_id, not a fresh reservation


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
            raise QuotaExceededError(
                f"Analysis allowance ({allowance}/period) exhausted for the current period."
            )
        return UsageReservation(id=result.id, replay=result.replay)

    async def consume_reservation(self, user_id: UUID, reservation_id: UUID) -> None:
        await usage_repository.consume(self._pool, user_id, reservation_id)

    async def release_reservation(self, user_id: UUID, reservation_id: UUID) -> None:
        await usage_repository.release(self._pool, user_id, reservation_id)
