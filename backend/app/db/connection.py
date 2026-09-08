import asyncpg
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_db_pool(dsn: Optional[str] = None) -> asyncpg.Pool:
    """
    Pool sizing and timeouts are configurable (Settings), not
    hardcoded, so a production deployment can tune them per environment
    without a code change. `timeout` here is asyncpg.connect's own
    per-connection connect timeout; `command_timeout` bounds any single
    query so a stuck query can't hold a pool connection forever.
    `application_name` shows up in `pg_stat_activity`, which matters
    once this runs behind PgBouncer or alongside other services on the
    same Postgres instance.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=dsn or settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            timeout=settings.db_pool_connect_timeout_seconds,
            command_timeout=settings.db_pool_command_timeout_seconds,
            server_settings={"application_name": "skincare-app-api"},
        )
        logger.info("Database pool initialized")
    return _pool


async def close_db_pool():
    """
    pool.close() (not .terminate()) waits for connections currently
    checked out to be released before closing them -- an in-flight
    request's connection isn't yanked out from under it on shutdown.
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


def get_db_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized -- call init_db_pool() at startup")
    return _pool
