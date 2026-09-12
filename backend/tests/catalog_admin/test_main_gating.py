"""app.catalog_admin.__main__ -- refuses to run at all when
CATALOG_ADMIN_ENABLED is not set, before ever attempting to open a
database connection."""
from app.catalog_admin.__main__ import _main
from app.config import settings
from app.db import connection as db_connection


async def test_refuses_to_run_when_catalog_admin_disabled(monkeypatch):
    monkeypatch.setattr(settings, "catalog_admin_enabled", False)
    code = await _main(["review-list"])
    assert code == 1
    assert db_connection._catalog_admin_pool is None
