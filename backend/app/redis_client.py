import redis.asyncio as redis
from app.config import settings

_redis_client: redis.Redis | None = None


async def init_redis() -> None:
    global _redis_client
    _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    await _redis_client.ping()  # fail fast at boot if unreachable


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()


def get_redis() -> redis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis not initialized")
    return _redis_client
