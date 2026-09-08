import redis.asyncio as redis
from app.config import settings

_redis_client: redis.Redis | None = None


async def init_redis() -> None:
    """
    Explicit connect/socket timeouts, rather than redis-py's implicit
    defaults -- under real load (or a busy CPU-bound test sandbox
    running mediapipe/opencv work on the same box), an unbounded
    socket wait turns a transient hiccup into an indefinite hang
    instead of the fast, well-defined failure get_current_user's 503
    fail-closed path already depends on.
    """
    global _redis_client
    _redis_client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )
    await _redis_client.ping()  # fail fast at boot if unreachable


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()


def get_redis() -> redis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis not initialized")
    return _redis_client
