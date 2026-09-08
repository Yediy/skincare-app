"""Phase 3: account state and immediate invalidation.

An access token issued before a user's account was disabled/deleted
must stop working immediately -- not just at its natural JWT expiry --
because get_current_user checks Postgres directly on every request.
"""


async def test_disabled_account_rejects_existing_access_token(client, db_pool):
    await client.post("/signup", json={"email": "disabletest@test.com", "password": "testpass123"})
    login = await client.post("/login", json={"email": "disabletest@test.com", "password": "testpass123"})
    access_token = login.json()["access_token"]

    # Sanity: the token works before the account is disabled.
    me_before = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_before.status_code == 200

    await db_pool.execute("UPDATE users SET is_active = false WHERE email = $1", "disabletest@test.com")

    me_after = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_after.status_code == 401


async def test_delete_account_route_rejects_own_subsequent_requests(client):
    await client.post("/signup", json={"email": "deletetest@test.com", "password": "testpass123"})
    login = await client.post("/login", json={"email": "deletetest@test.com", "password": "testpass123"})
    access_token = login.json()["access_token"]
    refresh_token = login.json()["refresh_token"]

    delete_resp = await client.delete("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert delete_resp.status_code == 200

    me_after = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_after.status_code == 401

    refresh_after = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert refresh_after.status_code == 401


async def test_deleted_at_alone_also_rejects(client, db_pool):
    """Covers the deleted_at column independently of is_active, in case
    a future code path sets one without the other."""
    await client.post("/signup", json={"email": "deletedattest@test.com", "password": "testpass123"})
    login = await client.post("/login", json={"email": "deletedattest@test.com", "password": "testpass123"})
    access_token = login.json()["access_token"]

    await db_pool.execute("UPDATE users SET deleted_at = now() WHERE email = $1", "deletedattest@test.com")

    me_after = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_after.status_code == 401
