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
