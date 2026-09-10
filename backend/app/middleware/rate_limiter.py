"""
Atomic Redis rate limiting (Phase 6 of the product/usage foundation
pass). A single Lua script does INCR + conditional EXPIRE + threshold
check in one round trip -- not the classic non-atomic

    count = GET key
    if count < limit: INCR key

which races: two concurrent requests can both read the same
under-limit count before either writes, and both proceed, letting the
limit be exceeded under real concurrent load. The Lua script runs as
one atomic operation on the Redis server, so there is no window
between "read the count" and "act on it" for two concurrent callers to
race through.

Algorithm: a **fixed window** counter, not a sliding window or token
bucket -- a deliberate, documented choice. A fixed window can allow up
to ~2x the configured limit across a single window-boundary instant in
the worst case (a burst just before the window resets, followed by
another just after), but needs no extra bookkeeping beyond one
INCR-able key per identity+policy+window and is atomic and correct
within each window by construction. That is sufficient for this
pass's actual threat model (abusive bursts, credential stuffing,
analysis-endpoint cost control) -- not a precision rate-limiting SLA.
A sliding-window log or token-bucket implementation would be a
reasonable future upgrade if the 2x-at-the-boundary behavior ever
actually matters, not a correctness requirement today.
"""
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

# KEYS[1] = the per-identity+policy+window counter key
# ARGV[1] = window_seconds
# ARGV[2] = max_requests
#
# The key's own TTL *is* the window: the first request in a new window
# creates it and sets its expiry; every request after that just
# increments it until it naturally expires, at which point the next
# request starts a fresh window. Returns {allowed (0/1), retry_after_seconds}.
_RATE_LIMIT_LUA = """
local current = redis.call("INCR", KEYS[1])
if current == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
local ttl = redis.call("TTL", KEYS[1])
if ttl < 0 then
    ttl = tonumber(ARGV[1])
end
if current > tonumber(ARGV[2]) then
    return {0, ttl}
end
return {1, ttl}
"""


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    max_requests: int
    window_seconds: int
    # What happens if Redis itself is unreachable when this policy's
    # check runs -- see USAGE_AND_RATE_LIMIT_ARCHITECTURE.md's "Rate
    # limit failure policy" section. True = fail open (allow the
    # request through, degrade rate limiting rather than availability).
    # False = fail closed (reject with 503, never let an unverifiable
    # request through an expensive or abuse-prone endpoint).
    fail_open: bool


AUTH_POLICY = RateLimitPolicy(
    "auth", settings.rate_limit_auth_max, settings.rate_limit_auth_window_seconds, fail_open=False,
)
ANALYSIS_POLICY = RateLimitPolicy(
    "analysis", settings.rate_limit_analysis_max, settings.rate_limit_analysis_window_seconds, fail_open=False,
)
GENERAL_POLICY = RateLimitPolicy(
    "general", settings.rate_limit_general_max, settings.rate_limit_general_window_seconds, fail_open=True,
)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class RedisUnavailableForRateLimit(Exception):
    """Raised (only by callers that explicitly ask for fail-closed
    behavior) when Redis is unreachable and the policy's fail_open is
    False. Kept distinct from RateLimitResult(allowed=False, ...) so a
    caller can tell "over the limit" apart from "couldn't check the
    limit at all" if it ever needs to (e.g. different response body),
    even though both currently map to the same HTTP status."""


class RateLimiter:
    """Thin wrapper around one Redis client. Stateless beyond that --
    every policy/identity combination is just a different Redis key,
    so one instance safely serves every policy."""

    def __init__(self, redis_client: Redis):
        self._redis = redis_client

    async def check(self, policy: RateLimitPolicy, identity: str) -> RateLimitResult:
        key = f"ratelimit:{policy.name}:{identity}"
        try:
            current, ttl = await self._redis.eval(
                _RATE_LIMIT_LUA, 1, key, policy.window_seconds, policy.max_requests
            )
        except RedisError:
            if policy.fail_open:
                return RateLimitResult(allowed=True, retry_after_seconds=0)
            raise RedisUnavailableForRateLimit(policy.name)
        return RateLimitResult(allowed=bool(int(current)), retry_after_seconds=int(ttl))


def resolve_client_ip(request: Request) -> str:
    """The direct TCP peer is authoritative unless it is itself a
    configured trusted proxy -- only then is X-Forwarded-For honored,
    and only its first (client-nearest) entry. An unlisted peer's
    X-Forwarded-For is never trusted: any client can set that header to
    an arbitrary value (including another real user's IP, or a
    different value on every request), so honoring it from an
    untrusted peer would let IP-based limiting be evaded trivially."""
    direct_ip = request.client.host if request.client else "unknown"
    if direct_ip in settings.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first_hop = forwarded.split(",")[0].strip()
            if first_hop:
                return first_hop
    return direct_ip


async def _enforce(policy: RateLimitPolicy, identity: str) -> None:
    from app.redis_client import get_redis

    limiter = RateLimiter(get_redis())
    try:
        result = await limiter.check(policy, identity)
    except RedisUnavailableForRateLimit:
        raise HTTPException(
            status_code=503,
            detail=f"Rate limiting for '{policy.name}' is temporarily unavailable; try again shortly.",
        )
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests.",
            headers={"Retry-After": str(result.retry_after_seconds)},
        )


def rate_limit_by_ip(policy: RateLimitPolicy):
    """FastAPI dependency factory for unauthenticated routes (no
    user_id exists yet to key by)."""

    async def _dependency(request: Request) -> None:
        await _enforce(policy, f"ip:{resolve_client_ip(request)}")

    return _dependency


def rate_limit_by_user(policy: RateLimitPolicy):
    """FastAPI dependency factory for authenticated routes. Depends on
    get_current_user itself (not just a raw user_id string the route
    already resolved) so this dependency is self-contained and usable
    on its own -- FastAPI de-duplicates get_current_user's result
    within one request regardless of how many dependencies reference
    it, so this does not re-run authentication twice per request."""
    from app.security.auth import get_current_user

    async def _dependency(user_id: str = Depends(get_current_user)) -> str:
        await _enforce(policy, f"user:{user_id}")
        return user_id

    return _dependency
