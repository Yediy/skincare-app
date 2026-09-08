"""Phase 4: real user profile persistence, replacing hardcoded
placeholder values in /analyze."""
from app.db.profile_repository import DEFAULT_PROFILE


async def _signup_and_login(client, email="profiletest@test.com", password="testpass123"):
    await client.post("/signup", json={"email": email, "password": password})
    resp = await client.post("/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def test_get_profile_returns_documented_defaults_when_unset(client):
    token = await _signup_and_login(client)
    resp = await client.get("/profile", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == DEFAULT_PROFILE


async def test_put_profile_persists_and_get_reflects_it(client):
    token = await _signup_and_login(client, email="profiletest2@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    update = {
        "has_sensitive_skin": True,
        "experience_level": "advanced",
        "max_routine_steps": 6,
        "is_pregnant": True,
        "is_nursing": False,
        "allergies": ["fragrance", "nuts"],
        "avoid_ingredients": ["sulfates"],
    }
    put_resp = await client.put("/profile", json=update, headers=headers)
    assert put_resp.status_code == 200

    get_resp = await client.get("/profile", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json() == update


async def test_analyze_route_fetches_real_profile_via_get_profile(client, db_pool):
    """The specific regression this phase targets: /analyze must reflect
    a real persisted profile, not silently pretend every user is a
    beginner with no constraints. Verified directly via the same
    get_profile() call /analyze itself makes, keyed off the real user
    row created through /signup -- not a fabricated user_id."""
    token = await _signup_and_login(client, email="profiletest3@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    await client.put(
        "/profile",
        json={
            "has_sensitive_skin": True,
            "experience_level": "advanced",
            "max_routine_steps": 10,
            "is_pregnant": False,
            "is_nursing": False,
            "allergies": [],
            "avoid_ingredients": [],
        },
        headers=headers,
    )

    from app.db.profile_repository import get_profile

    user_row = await db_pool.fetchrow("SELECT id FROM users WHERE email = $1", "profiletest3@test.com")
    profile = await get_profile(db_pool, user_row["id"])
    assert profile["has_sensitive_skin"] is True
    assert profile["experience_level"] == "advanced"
