"""Smoke tests: the app imports, and the health route responds through
a real request against the test app (which does require the test
Postgres/Redis instances to be up, since app startup wires to them via
the `client` fixture -- see conftest.py)."""


def test_app_imports():
    from app import main  # noqa: F401


async def test_health_endpoint_responds(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
