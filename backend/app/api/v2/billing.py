"""GET /api/v2/billing/status, POST /api/v2/billing/sync -- Mobile C2's
only new backend surface. Both routes are thin adapters over the
EXISTING, unmodified billing architecture
(app/domain/entitlement.py::build_entitlement_service,
app/domain/revenuecat_reconciliation_service.py) -- this module
contains no new billing business logic, no second RevenueCat REST
client, and no new entitlement-state machine.

Central invariant (BILLING_ARCHITECTURE.md, unchanged by this pass):
`user_entitlements` (via RevenueCatEntitlementService) is the ONLY
authority for premium access/analysis quota. A mobile client's local
RevenueCat CustomerInfo is never trusted here -- both routes derive
the user id exclusively from the authenticated bearer token
(`get_current_user`, via the existing `rate_limit_by_user` dependency)
and never accept a caller-supplied user id or entitlement claim.

`has_premium_access` recognizes exactly the two statuses
RevenueCatEntitlementService itself treats as paid access (`ACTIVE`,
`GRACE_PERIOD`) -- see that class's own docstring; this module does
not redefine or duplicate that rule, it reads the same
`user_entitlements` row through `app.db.revenuecat_repository.
get_entitlement` (a plain SELECT, safe on the ordinary `skincare_app`
runtime role -- see migration 9815eb266923's own docstring: only
INSERT/UPDATE/DELETE were revoked from that role, never SELECT) and
applies the identical predicate `RevenueCatEntitlementService.
get_analysis_allowance` uses internally.
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.db import revenuecat_repository, usage_repository
from app.db.connection import get_billing_db_pool, get_db_pool
from app.domain.entitlement import build_entitlement_service, current_period_key
from app.domain.revenuecat_reconciliation_service import (
    ReconciliationAPIError,
    RevenueCatAPIClient,
    RevenueCatReconciliationService,
)
from app.middleware.rate_limiter import BILLING_SYNC_POLICY, GENERAL_POLICY, rate_limit_by_user

router = APIRouter(prefix="/api/v2/billing", tags=["billing"])

logger = logging.getLogger(__name__)


class BillingStatusResponse(BaseModel):
    billing_enabled: bool
    app_user_id: str
    entitlement_identifier: str
    # None when billing is disabled -- there is no RevenueCat
    # environment concept to report truthfully in that case.
    environment: Optional[str] = None
    # None when no entitlement row exists yet for this user (never
    # seen a webhook/reconciliation for them) -- a legitimate,
    # expected state for a free-tier or brand-new user, never an error.
    projection_status: Optional[str] = None
    has_premium_access: bool
    will_renew: Optional[bool] = None
    expires_at: Optional[datetime] = None
    analysis_allowance: int
    period_key: str
    analyses_used_or_reserved: int
    analyses_remaining: int


class BillingSyncResponse(BillingStatusResponse):
    mismatch_found: bool
    corrected: bool


async def _compute_billing_status(user_id: UUID) -> BillingStatusResponse:
    """Read-only -- never reserves, consumes, or releases a quota slot,
    never writes to `user_entitlements`. Reused identically by both
    routes (status, and the fresh status returned after a sync) so
    there is exactly one place that assembles this projection."""
    pool = get_db_pool()
    period_key = current_period_key()

    # The SAME composition-root factory analyses.py/analysis_worker.py
    # use -- flipping settings.revenuecat_billing_enabled is still the
    # only switch that changes any quota decision anywhere in the app;
    # this route never re-derives allowance logic independently.
    entitlement_service = build_entitlement_service(pool)
    allowance = await entitlement_service.get_analysis_allowance(user_id)
    used = await usage_repository.get_current_period_usage_count(pool, user_id, period_key)
    remaining = max(0, allowance - used)

    if not settings.revenuecat_billing_enabled:
        return BillingStatusResponse(
            billing_enabled=False,
            app_user_id=str(user_id),
            entitlement_identifier=settings.revenuecat_entitlement_id,
            environment=None,
            projection_status=None,
            has_premium_access=False,
            will_renew=None,
            expires_at=None,
            analysis_allowance=allowance,
            period_key=period_key,
            analyses_used_or_reserved=used,
            analyses_remaining=remaining,
        )

    environment = "PRODUCTION" if settings.is_production else "SANDBOX"
    entitlement_row = await revenuecat_repository.get_entitlement(
        pool, user_id, settings.revenuecat_entitlement_id, "revenuecat", environment,
    )
    projection_status = entitlement_row["status"] if entitlement_row is not None else None
    has_premium_access = projection_status in ("ACTIVE", "GRACE_PERIOD")

    return BillingStatusResponse(
        billing_enabled=True,
        app_user_id=str(user_id),
        entitlement_identifier=settings.revenuecat_entitlement_id,
        environment=environment,
        projection_status=projection_status,
        has_premium_access=has_premium_access,
        will_renew=entitlement_row["will_renew"] if entitlement_row is not None else None,
        expires_at=entitlement_row["expires_at"] if entitlement_row is not None else None,
        analysis_allowance=allowance,
        period_key=period_key,
        analyses_used_or_reserved=used,
        analyses_remaining=remaining,
    )


@router.get("/status", response_model=BillingStatusResponse)
async def get_billing_status(
    user_id: str = Depends(rate_limit_by_user(GENERAL_POLICY)),
) -> BillingStatusResponse:
    """User id comes exclusively from the authenticated bearer token
    (via rate_limit_by_user -> get_current_user) -- there is no
    request body or query parameter this route reads at all, so there
    is no argument by which a caller could ask for another user's
    entitlement."""
    return await _compute_billing_status(UUID(user_id))


@router.post("/sync", response_model=BillingSyncResponse)
async def sync_billing_status(
    user_id: str = Depends(rate_limit_by_user(BILLING_SYNC_POLICY)),
) -> BillingSyncResponse:
    """Lets an authenticated mobile client, immediately after a
    purchase or restore, ask the server to reconcile against
    RevenueCat's own API right now rather than wait for webhook
    delivery -- reuses RevenueCatReconciliationService.reconcile_user()
    completely unmodified; this route creates no second RevenueCat
    REST implementation and accepts no client-supplied app_user_id or
    entitlement claim of any kind.

    On a RevenueCat API failure, reconcile_user() itself re-raises
    BEFORE writing anything to the local entitlement projection (see
    its own docstring) -- so this route's 503 response is a genuine
    "nothing changed, please retry" outcome, never a downgrade of an
    existing entitlement merely because the network call failed."""
    if not settings.revenuecat_billing_enabled:
        raise HTTPException(status_code=503, detail="RevenueCat billing is not enabled on this deployment.")

    if not settings.revenuecat_entitlement_resource_id:
        # Fail closed rather than call RevenueCat with an ambiguous
        # entitlement configuration -- see Settings.
        # revenuecat_entitlement_resource_id's own comment. Production
        # startup already refuses to boot in this state (app/config.py),
        # so this branch is realistically only reachable in a
        # misconfigured non-production deployment.
        logger.error("billing_sync_misconfigured: REVENUECAT_ENTITLEMENT_RESOURCE_ID is unset while billing is enabled")
        raise HTTPException(status_code=503, detail="RevenueCat billing is misconfigured on this deployment.")

    billing_pool = get_billing_db_pool()
    environment = "PRODUCTION" if settings.is_production else "SANDBOX"
    api_client = RevenueCatAPIClient(
        api_key=settings.revenuecat_api_key or "", project_id=settings.revenuecat_project_id or "",
    )
    reconciliation_service = RevenueCatReconciliationService(
        billing_pool, api_client,
        entitlement_identifier=settings.revenuecat_entitlement_id,
        entitlement_resource_id=settings.revenuecat_entitlement_resource_id,
        environment=environment,
    )

    try:
        outcome = await reconciliation_service.reconcile_user(UUID(user_id))
    except ReconciliationAPIError as e:
        logger.warning("billing_sync_failed user_id=%s error_code=%s", user_id, e.error_code)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not reach the billing provider right now ({e.error_code}). "
                "Your existing access is unchanged -- please try again shortly."
            ),
        )

    status = await _compute_billing_status(UUID(user_id))
    return BillingSyncResponse(
        **status.model_dump(), mismatch_found=outcome.mismatch_found, corrected=outcome.corrected,
    )
