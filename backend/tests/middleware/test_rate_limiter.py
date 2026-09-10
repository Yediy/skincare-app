"""Real Redis tests for app.middleware.rate_limiter (Phase 6/9 of the
product/usage foundation pass). Run against the real test Redis
instance (redis_client fixture), not mocked -- the whole point of this
module is that a single Lua script makes the check-and-increment
atomic, which a mock can't meaningfully prove.
"""
import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.middleware.rate_limiter import (
    RateLimiter,
    RateLimitPolicy,
    RedisUnavailableForRateLimit,
    rate_limit_by_ip,
    resolve_client_ip,
)


def _policy(name="test-policy", max_requests=5, window_seconds=60, fail_open=False):
    return RateLimitPolicy(name, max_requests, window_seconds, fail_open)


def _fake_request(client_host="1.2.3.4", headers=None):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "client": (client_host, 12345),
        "headers": raw_headers,
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


async def test_allows_requests_up_to_the_configured_max(redis_client):
    limiter = RateLimiter(redis_client)
    policy = _policy(max_requests=3)

    for _ in range(3):
        result = await limiter.check(policy, "identity-a")
        assert result.allowed is True

    denied = await limiter.check(policy, "identity-a")
    assert denied.allowed is False
    assert denied.retry_after_seconds > 0


async def test_different_identities_have_independent_limits(redis_client):
    limiter = RateLimiter(redis_client)
    policy = _policy(max_requests=1)

    assert (await limiter.check(policy, "identity-x")).allowed is True
    assert (await limiter.check(policy, "identity-x")).allowed is False
    # A different identity under the same policy is unaffected.
    assert (await limiter.check(policy, "identity-y")).allowed is True


async def test_different_policies_have_independent_limits_for_the_same_identity(redis_client):
    limiter = RateLimiter(redis_client)
    policy_a = _policy(name="policy-a", max_requests=1)
    policy_b = _policy(name="policy-b", max_requests=1)

    assert (await limiter.check(policy_a, "same-identity")).allowed is True
    assert (await limiter.check(policy_a, "same-identity")).allowed is False
    # policy-b's key is namespaced separately -- unaffected by policy-a's count.
    assert (await limiter.check(policy_b, "same-identity")).allowed is True


async def test_concurrent_requests_at_exact_limit_no_overrun(redis_client):
    """The actual atomicity proof: a real concurrent burst against a
    limit of 5 must allow exactly 5 through and deny exactly the rest
    -- never more than 5 allowed, which is what a non-atomic
    GET-then-INCR implementation would risk under real concurrency."""
    limiter = RateLimiter(redis_client)
    policy = _policy(max_requests=5)

    results = await asyncio.gather(*(limiter.check(policy, "burst-identity") for _ in range(20)))
    allowed = [r for r in results if r.allowed]
    denied = [r for r in results if not r.allowed]

    assert len(allowed) == 5
    assert len(denied) == 15


async def test_fail_open_policy_allows_when_redis_unreachable():
    from redis.asyncio import Redis

    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5, socket_timeout=0.5)
    limiter = RateLimiter(unreachable)
    policy = _policy(fail_open=True)

    result = await limiter.check(policy, "any-identity")
    assert result.allowed is True


async def test_fail_closed_policy_raises_when_redis_unreachable():
    from redis.asyncio import Redis

    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5, socket_timeout=0.5)
    limiter = RateLimiter(unreachable)
    policy = _policy(fail_open=False)

    with pytest.raises(RedisUnavailableForRateLimit):
        await limiter.check(policy, "any-identity")


def test_resolve_client_ip_uses_direct_peer_by_default():
    request = _fake_request(client_host="9.9.9.9", headers={"X-Forwarded-For": "1.1.1.1"})
    # 9.9.9.9 is not a configured trusted proxy -- the forwarded header
    # must be ignored entirely, not honored.
    assert resolve_client_ip(request) == "9.9.9.9"


def test_resolve_client_ip_honors_xff_only_from_trusted_proxy(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.1"])
    request = _fake_request(client_host="10.0.0.1", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    assert resolve_client_ip(request) == "203.0.113.7"


def test_resolve_client_ip_ignores_xff_from_untrusted_peer_even_when_some_proxy_is_trusted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.1"])
    # A different, untrusted peer claiming to be forwarded -- must not
    # be honored just because *some* proxy is configured as trusted.
    request = _fake_request(client_host="6.6.6.6", headers={"X-Forwarded-For": "203.0.113.7"})
    assert resolve_client_ip(request) == "6.6.6.6"


async def test_rate_limit_by_ip_dependency_raises_429_with_retry_after(monkeypatch, redis_client):
    import app.redis_client as redis_module

    # _enforce() (called by the dependency below) resolves its Redis
    # client through app.redis_client.get_redis(), the same
    # module-global the real app uses -- pointing it at the test
    # fixture's already-isolated client avoids managing that global's
    # init/teardown lifecycle directly in this test.
    monkeypatch.setattr(redis_module, "_redis_client", redis_client)

    policy = _policy(name="dependency-test", max_requests=1)
    dependency = rate_limit_by_ip(policy)
    request = _fake_request(client_host="55.55.55.55")

    await dependency(request)  # first call: allowed, no exception

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request)

    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers
