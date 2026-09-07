import asyncpg
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/skincare"

_pool: Optional[asyncpg.Pool] = None


async def init_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=2, max_size=10)
        logger.info("Database pool initialized")
    return _pool


async def close_db_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


def get_db_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized -- call init_db_pool() at startup")
    return _pool
