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
        "skin_goals": ["EVENNESS_TONE", "REDNESS_CONTROL"],
    }
    put_resp = await client.put("/profile", json=update, headers=headers)
    assert put_resp.status_code == 200

    get_resp = await client.get("/profile", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json() == {**update, "profile_set": True}


async def test_get_profile_distinguishes_never_set_from_explicit_defaults(client):
    """Mobile V1 foundation: the onboarding bootstrap decision
    ("authenticated + required profile incomplete -> onboarding
    profile") needs to tell "never saved a profile" apart from "saved
    a profile that happens to equal the defaults" -- both must not
    look identical to a client relying on backend truth."""
    token = await _signup_and_login(client, email="profiletest4@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    unset_resp = await client.get("/profile", headers=headers)
    assert unset_resp.json()["profile_set"] is False

    put_resp = await client.put(
        "/profile",
        json={
            "has_sensitive_skin": False,
            "experience_level": "beginner",
            "max_routine_steps": 10,
            "is_pregnant": False,
            "is_nursing": False,
            "allergies": [],
            "avoid_ingredients": [],
            "skin_goals": [],
        },
        headers=headers,
    )
    assert put_resp.status_code == 200

    set_resp = await client.get("/profile", headers=headers)
    assert set_resp.json()["profile_set"] is True


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


# --- skin_goals vocabulary enforcement (mobile V1 repair pass) --------
#
# Mobile's own SKIN_GOAL_OPTIONS restricts *its* UI to a subset of
# app.domain.priorities.PRIORITIES, but the HTTP API is what's actually
# authoritative -- a client is not the only thing that can ever call
# PUT /profile, and skin_goals was previously an unconstrained
# List[str] accepting anything.

_BASE_PROFILE_UPDATE = {
    "has_sensitive_skin": False,
    "experience_level": "beginner",
    "max_routine_steps": 10,
    "is_pregnant": False,
    "is_nursing": False,
    "allergies": [],
    "avoid_ingredients": [],
}


async def test_put_profile_accepts_known_skin_goals(client):
    token = await _signup_and_login(client, email="skingoals-known@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        "/profile",
        json={**_BASE_PROFILE_UPDATE, "skin_goals": ["EVENNESS_TONE", "REDNESS_CONTROL"]},
        headers=headers,
    )
    assert resp.status_code == 200


async def test_put_profile_rejects_unknown_skin_goal(client):
    token = await _signup_and_login(client, email="skingoals-unknown@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        "/profile",
        json={**_BASE_PROFILE_UPDATE, "skin_goals": ["NOT_A_REAL_GOAL"]},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_put_profile_rejects_mixture_of_valid_and_unknown_skin_goals(client):
    token = await _signup_and_login(client, email="skingoals-mixed@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        "/profile",
        json={**_BASE_PROFILE_UPDATE, "skin_goals": ["EVENNESS_TONE", "MADE_UP_GOAL"]},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_put_profile_rejects_oversized_skin_goals_list(client):
    from app.domain.priorities import PRIORITIES

    token = await _signup_and_login(client, email="skingoals-oversized@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    # More entries than distinct real goals exist -- necessarily
    # invalid regardless of content, and cheap to construct without
    # hardcoding today's exact PRIORITIES count in the test itself.
    oversized = list(PRIORITIES.keys()) + ["EVENNESS_TONE"]
    resp = await client.put(
        "/profile",
        json={**_BASE_PROFILE_UPDATE, "skin_goals": oversized},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_put_profile_rejects_duplicate_skin_goals(client):
    token = await _signup_and_login(client, email="skingoals-duplicate@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        "/profile",
        json={**_BASE_PROFILE_UPDATE, "skin_goals": ["EVENNESS_TONE", "EVENNESS_TONE"]},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_get_profile_returns_persisted_valid_skin_goals(client):
    token = await _signup_and_login(client, email="skingoals-roundtrip@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    await client.put(
        "/profile",
        json={**_BASE_PROFILE_UPDATE, "skin_goals": ["OIL_CONTROL", "TEXTURE_SMOOTHING"]},
        headers=headers,
    )

    resp = await client.get("/profile", headers=headers)
    assert resp.status_code == 200
    assert set(resp.json()["skin_goals"]) == {"OIL_CONTROL", "TEXTURE_SMOOTHING"}
