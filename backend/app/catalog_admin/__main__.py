"""Entry point: `python -m app.catalog_admin <command> ...`. Refuses
to run at all when CATALOG_ADMIN_ENABLED is not set -- an operator
must explicitly opt in per environment, the same "off by default"
posture async_image_storage_enabled/revenuecat_billing_enabled use.
Connects through the dedicated `skincare_catalog_admin` role
(CATALOG_ADMIN_DATABASE_URL, migration 13c1fff1867e) -- never the
ordinary `skincare_app` role, which cannot write any catalog table at
all.
"""
import asyncio
import logging
import sys

from app.catalog_admin.cli import run
from app.config import settings
from app.db.connection import close_catalog_admin_db_pool, init_catalog_admin_db_pool


async def _main(argv) -> int:
    if not settings.catalog_admin_enabled:
        print(
            "CATALOG_ADMIN_ENABLED is not set -- refusing to run the catalog admin CLI.",
            file=sys.stderr,
        )
        return 1

    pool = await init_catalog_admin_db_pool()
    try:
        return await run(argv, pool)
    finally:
        await close_catalog_admin_db_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(_main(sys.argv[1:])))
