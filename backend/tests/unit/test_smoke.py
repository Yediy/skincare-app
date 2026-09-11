"""Smoke tests: the app imports, and the health route responds through
a real request against the test app (which does require the test
Postgres/Redis instances to be up, since app startup wires to them via
the `client` fixture -- see conftest.py)."""


def test_app_imports():
    from app import main  # noqa: F401


async def test_liveness_endpoint_responds(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_endpoint_passes_when_db_and_redis_are_up(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "ok"}


async def test_readiness_fails_closed_when_database_is_unreachable(client, monkeypatch):
    from app.db import connection as db_connection

    class _DeadPool:
        async def fetchval(self, *args, **kwargs):
            raise ConnectionRefusedError("simulated database outage")

    real_pool = db_connection._pool
    db_connection._pool = _DeadPool()
    try:
        response = await client.get("/health/ready")
    finally:
        db_connection._pool = real_pool

    assert response.status_code == 503
    body = response.json()["detail"]
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "unavailable: ConnectionRefusedError"
    # Liveness must be unaffected by the same outage -- it isn't
    # supposed to depend on the database at all.
    live_response = await client.get("/health/live")
    assert live_response.status_code == 200


async def test_readiness_omits_billing_check_when_billing_disabled(client):
    """revenuecat_billing_enabled defaults to False -- an unconfigured
    deployment's readiness contract must stay exactly what it was
    before the billing check existed, not gain a phantom third check."""
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_readiness_includes_billing_database_when_billing_enabled_and_reachable(client, monkeypatch):
    """app_instance (conftest.py) already wires a real, reachable
    billing_db_pool -- enabling the flag is enough to prove the check
    reports ok through it, no additional stubbing needed."""
    from app.config import settings

    monkeypatch.setattr(settings, "revenuecat_billing_enabled", True)
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["billing_database"] == "ok"


async def test_readiness_fails_closed_when_billing_enabled_but_pool_unavailable(client, monkeypatch):
    """Billing enabled but the billing pool isn't actually up (never
    initialized, or since died) must fail the whole probe -- otherwise
    this instance would report ready while a real RevenueCat webhook
    delivery is guaranteed to fail."""
    from app.config import settings
    from app.db import connection as db_connection

    monkeypatch.setattr(settings, "revenuecat_billing_enabled", True)
    real_billing_pool = db_connection._billing_pool
    db_connection._billing_pool = None
    try:
        response = await client.get("/health/ready")
    finally:
        db_connection._billing_pool = real_billing_pool

    assert response.status_code == 503
    body = response.json()["detail"]
    assert body["status"] == "not_ready"
    assert body["checks"]["billing_database"] == "unavailable: RuntimeError"
    # The ordinary checks are unaffected by the billing pool's absence.
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"
