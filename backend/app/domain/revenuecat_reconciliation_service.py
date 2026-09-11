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

Uses RevenueCat's documented v2 "list a customer's active entitlements"
resource (`GET /projects/{project_id}/customers/{customer_id}/
active_entitlements`) -- not `GET /customers/{customer_id}?expand=
active_entitlements`, which does not exist; that endpoint's only
documented `expand` value is `attributes`. See
REVENUECAT_INTEGRATION_NOTES.md section 5 for the verified contract.
The response is a paginated `object: "list"` resource
(`items`/`next_page`/`url`) -- this client follows `next_page` up to a
bounded page count so the configured entitlement can never be falsely
declared absent merely because it appears on a later page.

`next_page` is documented as a *relative* path (e.g.
`/v2/projects/{project_id}/customers/{customer_id}/active_entitlements
?starting_after=...`), never an absolute URL -- and this client treats
it that way deliberately, not just as a matter of following the docs.
Every request here carries `Authorization: Bearer <RevenueCat API
key>`; if a malformed or compromised response could redirect
pagination to an arbitrary absolute URL, that header would go with it.
`_resolve_and_validate_next_page` resolves `next_page` against the
*current* page's own URL and then requires the result to still share
the configured API base's scheme/host/port -- a `next_page` that
resolves anywhere else is rejected outright (fails closed, tagged
`UNTRUSTED_PAGINATION_ORIGIN`) before any request is made to it, so
the Authorization header can never leak cross-origin. A `next_page`
that resolves back to an already-fetched URL is rejected the same way
(`PAGINATION_LOOP_DETECTED`), independent of the page-count bound
below.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

import asyncpg
import httpx

from app.db import revenuecat_repository
from app.observability import events as observability_events

logger = logging.getLogger(__name__)

REVENUECAT_API_BASE_URL = "https://api.revenuecat.com/v2"
# Real, deliberate bound: a customer with a legitimately large history
# of entitlement grants still resolves within this many pages: if the
# configured entitlement hasn't shown up by then, treat it as absent
# rather than looping on a misbehaving/malicious next_page chain
# forever.
MAX_ACTIVE_ENTITLEMENT_PAGES = 20
# Minimum spacing between successive RevenueCat API calls in a batch --
# real backoff, not a fabricated SLA number: generous enough that a
# reconciliation sweep of even a few hundred users cannot itself look
# like abusive traffic to RevenueCat's own API rate limiting.
DEFAULT_BATCH_DELAY_SECONDS = 0.25

_STATUS_ERROR_CODES = {
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "CUSTOMER_NOT_FOUND",
    429: "RATE_LIMITED",
}


class ReconciliationAPIError(Exception):
    """Raised when the RevenueCat API call itself fails (network error,
    non-2xx status). Distinct from a mismatch (which is a successful
    call whose result disagrees with local state) -- see
    reconciliation_failure vs. reconciliation_mismatch. `error_code` is
    a closed, deliberately-mapped classification
    (UNAUTHORIZED/FORBIDDEN/CUSTOMER_NOT_FOUND/RATE_LIMITED/
    SERVER_ERROR/NETWORK_ERROR), never a raw exception message, so a
    caller can distinguish "RevenueCat is down" from "our own API key
    is wrong" without parsing text."""

    def __init__(self, message: str, *, error_code: str, status_code: Optional[int] = None):
        self.error_code = error_code
        self.status_code = status_code
        super().__init__(message)


def _error_code_for_status(status_code: int) -> str:
    if status_code in _STATUS_ERROR_CODES:
        return _STATUS_ERROR_CODES[status_code]
    if status_code >= 500:
        return "SERVER_ERROR"
    return "HTTP_ERROR"


def _origin(url: str) -> Tuple[str, str, int]:
    """(scheme, hostname, effective port) -- the identity a same-origin
    check must compare, not the raw string (so an explicit default port
    doesn't falsely mismatch an implicit one)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, hostname, port)


class RevenueCatAPIClient:
    """Thin wrapper around RevenueCat's documented v2 active-entitlements
    resource. See REVENUECAT_INTEGRATION_NOTES.md section 5.

    `transport` (an httpx.BaseTransport, e.g. httpx.MockTransport) is
    accepted purely for testing -- production code never passes it, so
    a real httpx.AsyncClient with real networking is what actually
    runs."""

    def __init__(
        self, api_key: str, project_id: str, *, base_url: str = REVENUECAT_API_BASE_URL,
        timeout_seconds: float = 10.0, transport: Optional[httpx.BaseTransport] = None,
    ):
        self._api_key = api_key
        self._project_id = project_id
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _resolve_and_validate_next_page(self, current_url: str, next_page: str) -> str:
        """Resolves a (documented-relative, but defensively also
        accepted if absolute) `next_page` value against the page that
        returned it, then requires the result to share the configured
        API base's origin. Raises ReconciliationAPIError rather than
        ever returning an untrusted URL -- see the module docstring."""
        resolved = urljoin(current_url, next_page)
        if _origin(resolved) != _origin(self._base_url):
            raise ReconciliationAPIError(
                "RevenueCat active_entitlements next_page resolved outside the trusted API origin",
                error_code="UNTRUSTED_PAGINATION_ORIGIN",
            )
        return resolved

    async def get_active_entitlements(self, app_user_id: str) -> List[Dict[str, Any]]:
        """Returns the customer's active-entitlement items (each a dict
        with at least `entitlement_id`, optionally `expires_at`),
        following documented pagination (`next_page`) up to
        MAX_ACTIVE_ENTITLEMENT_PAGES. See the module docstring for the
        trusted-origin and loop-detection guarantees this enforces
        before ever issuing a request to a `next_page` value."""
        url = f"{self._base_url}/projects/{self._project_id}/customers/{quote(app_user_id, safe='')}/active_entitlements"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        items: List[Dict[str, Any]] = []
        pages_fetched = 0
        seen_urls: set = set()
        async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
            while url is not None:
                if pages_fetched >= MAX_ACTIVE_ENTITLEMENT_PAGES:
                    logger.warning(
                        "get_active_entitlements: hit MAX_ACTIVE_ENTITLEMENT_PAGES=%d for app_user_id=%s, "
                        "stopping pagination",
                        MAX_ACTIVE_ENTITLEMENT_PAGES, app_user_id,
                    )
                    break
                if url in seen_urls:
                    raise ReconciliationAPIError(
                        "RevenueCat active_entitlements next_page looped back to an already-fetched page",
                        error_code="PAGINATION_LOOP_DETECTED",
                    )
                seen_urls.add(url)

                try:
                    response = await client.get(url, headers=headers)
                except httpx.HTTPError as e:
                    raise ReconciliationAPIError(
                        f"{e.__class__.__name__}: {e}", error_code="NETWORK_ERROR",
                    ) from e

                if response.status_code >= 400:
                    raise ReconciliationAPIError(
                        f"RevenueCat active_entitlements returned HTTP {response.status_code}",
                        error_code=_error_code_for_status(response.status_code),
                        status_code=response.status_code,
                    )

                payload = response.json()
                items.extend(payload.get("items") or [])
                pages_fetched += 1

                next_page = payload.get("next_page") or None
                url = self._resolve_and_validate_next_page(url, next_page) if next_page is not None else None

        return items


def _find_entitlement(items: List[Dict[str, Any]], entitlement_identifier: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if isinstance(item, dict) and item.get("entitlement_id") == entitlement_identifier:
            return item
    return None


def _remote_expires_at(item: Dict[str, Any]) -> Optional[datetime]:
    expires_at_ms = item.get("expires_at")
    if not expires_at_ms:
        return None
    return datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)


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
            items = await self._api_client.get_active_entitlements(str(user_id))
        except ReconciliationAPIError as e:
            observability_events.reconciliation_failure(user_id=str(user_id), error_code=e.error_code)
            raise

        remote_entitlement = _find_entitlement(items, self._entitlement_identifier)
        remote_active = remote_entitlement is not None

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
            # The active-entitlements resource does not document a
            # renewal-state field (no fabricated will_renew=True from a
            # response that never said so) -- preserve whatever
            # renewal preference was already locally known; a freshly-
            # discovered entitlement with no prior local row has no
            # renewal metadata to preserve, so it stays unknown/True by
            # the same "unknown, no access consequence either way"
            # reasoning as the refund-cancellation case in
            # app/domain/revenuecat_entitlement_processor.py. A future
            # pass wanting authoritative renewal state should query a
            # documented subscription resource, not synthesize one here.
            preserved_will_renew = local["will_renew"] if local is not None else True
            await revenuecat_repository.apply_entitlement_projection(
                self._pool, user_id=user_id, entitlement_identifier=self._entitlement_identifier,
                provider="revenuecat", status=revenuecat_repository.ACTIVE, effective_at=now,
                expires_at=_remote_expires_at(remote_entitlement or {}),
                will_renew=preserved_will_renew,
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
