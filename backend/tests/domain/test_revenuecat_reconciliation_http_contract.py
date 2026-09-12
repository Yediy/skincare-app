"""RevenueCatAPIClient's actual HTTP contract, exercised through
httpx.MockTransport -- not the duck-typed FakeAPIClient used by
tests/domain/test_revenuecat_reconciliation_service.py (which tests
RevenueCatReconciliationService's own logic, not the transport). These
tests are the only place in this suite that proves the client hits the
right URL, sends the right header, parses the real documented response
shape, follows pagination, and maps HTTP failures deliberately -- no
real network access is used or required.
"""
import json

import httpx
import pytest

from app.domain.revenuecat_reconciliation_service import (
    MAX_ACTIVE_ENTITLEMENT_PAGES,
    REVENUECAT_API_BASE_URL,
    ReconciliationAPIError,
    RevenueCatAPIClient,
)

PROJECT_ID = "proj_test123"
API_KEY = "sk_test_only_never_a_real_revenuecat_key_0123456789"


def _client(handler) -> RevenueCatAPIClient:
    transport = httpx.MockTransport(handler)
    return RevenueCatAPIClient(API_KEY, PROJECT_ID, transport=transport)


async def test_requests_the_documented_active_entitlements_url_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"object": "list", "items": [], "next_page": None})

    client = _client(handler)
    await client.get_active_entitlements("user-abc-123")

    assert captured["url"] == (
        f"{REVENUECAT_API_BASE_URL}/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements"
    )
    assert captured["authorization"] == f"Bearer {API_KEY}"


async def test_url_encodes_the_app_user_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # request.url.path is httpx's own *decoded* view -- the raw
        # wire path (raw_path) is what actually proves encoding
        # happened before the request was sent.
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(200, json={"object": "list", "items": [], "next_page": None})

    client = _client(handler)
    await client.get_active_entitlements("user with spaces/slash")

    assert b"user with spaces/slash" not in captured["raw_path"]
    assert b"user%20with%20spaces%2Fslash" in captured["raw_path"]


async def test_parses_the_real_documented_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "items": [
                    {"object": "customer.active_entitlement", "entitlement_id": "premium", "expires_at": 1658399423658},
                ],
                "next_page": None,
                "url": "/v2/projects/proj_test123/customers/user-abc-123/active_entitlements",
            },
        )

    client = _client(handler)
    items = await client.get_active_entitlements("user-abc-123")

    assert len(items) == 1
    assert items[0]["entitlement_id"] == "premium"
    assert items[0]["expires_at"] == 1658399423658


async def test_follows_pagination_via_next_page():
    """RevenueCat's documented `next_page` form is a path RELATIVE to
    the API host, e.g. `/v2/projects/.../active_entitlements?starting_
    after=...` -- never an absolute URL. This is the real contract
    (see REVENUECAT_INTEGRATION_NOTES.md section 5), not a fabricated
    one -- a client that only worked against a fabricated absolute
    `next_page` would break against RevenueCat's real API."""
    page_1_url = f"{REVENUECAT_API_BASE_URL}/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements"
    relative_next_page = f"/v2/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements?starting_after=cursor123"
    page_2_url = f"{REVENUECAT_API_BASE_URL}/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements?starting_after=cursor123"
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if str(request.url) == page_1_url:
            return httpx.Response(
                200,
                json={"object": "list", "items": [{"entitlement_id": "other_ent"}], "next_page": relative_next_page},
            )
        return httpx.Response(
            200, json={"object": "list", "items": [{"entitlement_id": "premium", "expires_at": None}], "next_page": None},
        )

    client = _client(handler)
    items = await client.get_active_entitlements("user-abc-123")

    assert requested_urls == [page_1_url, page_2_url]
    assert [item["entitlement_id"] for item in items] == ["other_ent", "premium"]


async def test_pagination_is_bounded_by_a_maximum_page_count():
    """The configured entitlement being on a very late page must not
    turn into an infinite/unbounded fetch loop -- a deliberate cap
    stops it instead. Each next_page is a distinct, documented-relative
    path so this never trips loop detection before hitting the cap."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        next_page = f"/v2/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements?page={call_count['n']}"
        return httpx.Response(200, json={"object": "list", "items": [], "next_page": next_page})

    client = _client(handler)
    items = await client.get_active_entitlements("user-abc-123")

    assert items == []
    assert call_count["n"] == MAX_ACTIVE_ENTITLEMENT_PAGES


async def test_cross_origin_next_page_is_rejected_without_leaking_authorization():
    """A malformed or compromised response pointing `next_page` at an
    absolute, cross-origin URL must never be followed -- doing so would
    carry the RevenueCat Authorization header to an arbitrary host."""
    page_1_url = f"{REVENUECAT_API_BASE_URL}/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements"
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert "evil.example" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "items": [{"entitlement_id": "other_ent"}],
                "next_page": "https://evil.example/steal",
            },
        )

    client = _client(handler)
    with pytest.raises(ReconciliationAPIError) as exc_info:
        await client.get_active_entitlements("user-abc-123")

    assert exc_info.value.error_code == "UNTRUSTED_PAGINATION_ORIGIN"
    # Only the legitimate first page was ever requested -- the client
    # never issued a request to evil.example, so no Authorization
    # header (or anything else) could have reached it.
    assert requested == [page_1_url]


async def test_repeating_next_page_loop_is_detected_and_fails_closed():
    """A next_page that resolves back to an already-fetched page (a
    misbehaving or malicious provider response) must not spin forever
    -- it fails closed with a deliberate error classification instead,
    independent of the page-count bound."""
    relative_next_page = f"/v2/projects/{PROJECT_ID}/customers/user-abc-123/active_entitlements"
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={"object": "list", "items": [{"entitlement_id": "other_ent"}], "next_page": relative_next_page},
        )

    client = _client(handler)
    with pytest.raises(ReconciliationAPIError) as exc_info:
        await client.get_active_entitlements("user-abc-123")

    assert exc_info.value.error_code == "PAGINATION_LOOP_DETECTED"
    # Fetched the looping page exactly once before detecting the repeat
    # -- not zero (it's a legitimate page) and not unbounded.
    assert call_count["n"] == 1


@pytest.mark.parametrize(
    "status_code,expected_error_code",
    [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "CUSTOMER_NOT_FOUND"), (429, "RATE_LIMITED"), (500, "SERVER_ERROR"), (503, "SERVER_ERROR")],
)
async def test_http_error_status_codes_are_mapped_deliberately(status_code, expected_error_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "error"})

    client = _client(handler)
    with pytest.raises(ReconciliationAPIError) as exc_info:
        await client.get_active_entitlements("user-abc-123")

    assert exc_info.value.error_code == expected_error_code
    assert exc_info.value.status_code == status_code


async def test_network_error_is_wrapped_as_reconciliation_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated connect timeout", request=request)

    client = _client(handler)
    with pytest.raises(ReconciliationAPIError) as exc_info:
        await client.get_active_entitlements("user-abc-123")

    assert exc_info.value.error_code == "NETWORK_ERROR"
