"""Reconciliation against RevenueCat's own REST API (Section 13 of the
billing brief) -- the *only* module in this pass that calls out to
RevenueCat over the network. Everything on the ordinary request path
(RevenueCatEntitlementService, the webhook processor) reads/writes
only the local Postgres projection; this module exists purely to
correct that projection when it may have drifted (a missed webhook, a
webhook that failed to process, an alias/transfer discrepancy) -- see
BILLING_ARCHITECTURE.md's "Provider failure / staleness policy"
section for the acceptable-staleness contract this implies.

Not wired into any HTTP route this pass (Section 13: "do not reconcile
every user on every request"). `reconcile_user` is single-user,
`reconcile_batch` is a bounded batch suitable for a future scheduled
job (cron/worker) -- neither is invoked automatically by this pass;
that scheduling wiring is deliberately left for whoever operates this
in production, tracked as DEFERRED in OPEN_ENGINEERING_ITEMS.md.

Response parsing is defensive by design: REVENUECAT_INTEGRATION_NOTES.md
section 5 documents that the exact v2 "get a customer" response shape
could not be reproduced verbatim from the official docs in this pass's
research. An unexpected/missing field is treated as "no active
entitlement found" (safe default -- never silently assume paid access
from an unparseable response) rather than raised as a parsing
exception, and is exactly the kind of thing `reconciliation_mismatch`
exists to surface for a human to look at.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import UUID

import asyncpg
import httpx

from app.db import revenuecat_repository
from app.observability import events as observability_events

logger = logging.getLogger(__name__)

REVENUECAT_API_BASE_URL = "https://api.revenuecat.com/v2"
_ACTIVE_STATUS_VALUES = {"active", "trialing", "in_grace_period"}
# Minimum spacing between successive RevenueCat API calls in a batch --
# real backoff, not a fabricated SLA number: generous enough that a
# reconciliation sweep of even a few hundred users cannot itself look
# like abusive traffic to RevenueCat's own API rate limiting.
DEFAULT_BATCH_DELAY_SECONDS = 0.25


class ReconciliationAPIError(Exception):
    """Raised when the RevenueCat API call itself fails (network error,
    non-2xx status). Distinct from a mismatch (which is a successful
    call whose result disagrees with local state) -- see
    reconciliation_failure vs. reconciliation_mismatch."""


class RevenueCatAPIClient:
    """Thin wrapper around the one RevenueCat REST endpoint this pass
    needs -- GET a customer's active entitlements. See
    REVENUECAT_INTEGRATION_NOTES.md section 5."""

    def __init__(self, api_key: str, project_id: str, *, base_url: str = REVENUECAT_API_BASE_URL, timeout_seconds: float = 10.0):
        self._api_key = api_key
        self._project_id = project_id
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    async def get_customer(self, app_user_id: str) -> Dict[str, Any]:
        url = f"{self._base_url}/projects/{self._project_id}/customers/{quote(app_user_id, safe='')}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(url, params={"expand": "active_entitlements"}, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise ReconciliationAPIError(f"{e.__class__.__name__}: {e}") from e


def _extract_active_entitlement(customer: Dict[str, Any], entitlement_identifier: str) -> Optional[Dict[str, Any]]:
    entitlements = customer.get("active_entitlements") or customer.get("entitlements") or {}
    items = entitlements.get("items") if isinstance(entitlements, dict) else entitlements
    if not items:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("entitlement_id") == entitlement_identifier or item.get("id") == entitlement_identifier:
            return item
    return None


def _remote_gives_access(entitlement: Optional[Dict[str, Any]]) -> bool:
    if entitlement is None:
        return False
    if "gives_access" in entitlement:
        return bool(entitlement["gives_access"])
    return str(entitlement.get("status", "")).lower() in _ACTIVE_STATUS_VALUES


def _remote_expires_at(entitlement: Dict[str, Any]) -> Optional[datetime]:
    if "expiration_at_ms" in entitlement and entitlement["expiration_at_ms"]:
        return datetime.fromtimestamp(entitlement["expiration_at_ms"] / 1000, tz=timezone.utc)
    for key in ("expires_date", "expires_at"):
        value = entitlement.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
    return None


@dataclass(frozen=True)
class ReconciliationOutcome:
    user_id: UUID
    mismatch_found: bool
    corrected: bool
    remote_active: bool
    local_status: Optional[str]


class RevenueCatReconciliationService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        api_client: RevenueCatAPIClient,
        *,
        entitlement_identifier: str,
        environment: str,
    ):
        self._pool = pool
        self._api_client = api_client
        self._entitlement_identifier = entitlement_identifier
        self._environment = environment

    async def reconcile_user(self, user_id: UUID) -> ReconciliationOutcome:
        local = await revenuecat_repository.get_entitlement(
            self._pool, user_id, self._entitlement_identifier, "revenuecat", self._environment,
        )
        local_status = local["status"] if local is not None else None
        local_active = local_status in ("ACTIVE", "GRACE_PERIOD")

        try:
            customer = await self._api_client.get_customer(str(user_id))
        except ReconciliationAPIError as e:
            observability_events.reconciliation_failure(user_id=str(user_id), error_code=e.__class__.__name__)
            raise

        remote_entitlement = _extract_active_entitlement(customer, self._entitlement_identifier)
        remote_active = _remote_gives_access(remote_entitlement)

        if local_active == remote_active:
            observability_events.reconciliation_success(user_id=str(user_id), mismatch_found=False)
            return ReconciliationOutcome(
                user_id=user_id, mismatch_found=False, corrected=False,
                remote_active=remote_active, local_status=local_status,
            )

        observability_events.reconciliation_mismatch(
            user_id=str(user_id), local_status=local_status, remote_active=remote_active,
        )

        now = datetime.now(timezone.utc)
        if remote_active:
            await revenuecat_repository.apply_entitlement_projection(
                self._pool, user_id=user_id, entitlement_identifier=self._entitlement_identifier,
                provider="revenuecat", status=revenuecat_repository.ACTIVE, effective_at=now,
                expires_at=_remote_expires_at(remote_entitlement or {}),
                will_renew=bool((remote_entitlement or {}).get("auto_renewal_status", True)),
                environment=self._environment, source_event_id=None, provider_customer_id=str(user_id),
                last_provider_event_at=now,
            )
        else:
            await revenuecat_repository.apply_entitlement_projection(
                self._pool, user_id=user_id, entitlement_identifier=self._entitlement_identifier,
                provider="revenuecat", status=revenuecat_repository.EXPIRED, effective_at=now,
                expires_at=now, will_renew=False, environment=self._environment, source_event_id=None,
                provider_customer_id=str(user_id), last_provider_event_at=now,
            )

        observability_events.reconciliation_success(user_id=str(user_id), mismatch_found=True)
        return ReconciliationOutcome(
            user_id=user_id, mismatch_found=True, corrected=True,
            remote_active=remote_active, local_status=local_status,
        )

    async def reconcile_batch(
        self, user_ids: List[UUID], *, delay_seconds: float = DEFAULT_BATCH_DELAY_SECONDS,
    ) -> List[ReconciliationOutcome]:
        """Bounded batch reconciliation suitable for a future scheduled
        job -- not itself scheduled by this pass (see module
        docstring). One user's API failure is logged
        (reconciliation_failure) and skipped, never aborts the rest of
        the batch."""
        outcomes: List[ReconciliationOutcome] = []
        for index, user_id in enumerate(user_ids):
            try:
                outcomes.append(await self.reconcile_user(user_id))
            except ReconciliationAPIError:
                logger.warning("reconciliation_batch: user_id=%s failed, continuing batch", user_id, exc_info=True)
            if index < len(user_ids) - 1:
                await asyncio.sleep(delay_seconds)
        return outcomes
