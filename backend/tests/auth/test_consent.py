"""Phase 5: append-only consent ledger, gating /analyze.

Uses garbage image bytes for the "allowed" cases -- the point of these
tests is whether the consent gate lets the request through to the CV
pipeline (a 422 from bad image bytes) rather than blocking it upfront
with a 403, not whether a full analysis succeeds.
"""
from app.db.consent_repository import REQUIRED_POLICY_VERSION


async def _signup_and_login(client, email):
    await client.post("/signup", json={"email": email, "password": "testpass123"})
    resp = await client.post("/login", json={"email": email, "password": "testpass123"})
    return resp.json()["access_token"]


async def test_analyze_denied_without_any_consent(client):
    token = await _signup_and_login(client, "noconsent@test.com")
    resp = await client.post(
        "/analyze", json={"image_base64": "aGVsbG8="}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_analyze_allowed_with_valid_current_consent(client):
    token = await _signup_and_login(client, "validconsent@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    grant = await client.post(
        "/consent",
        json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "facial skin analysis"},
        headers=headers,
    )
    assert grant.status_code == 200

    resp = await client.post("/analyze", json={"image_base64": "aGVsbG8="}, headers=headers)
    # Consent gate passed -- it now fails for image-decoding reasons,
    # not a 403.
    assert resp.status_code == 422


async def test_old_policy_version_requires_reconsent(client):
    token = await _signup_and_login(client, "oldpolicy@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/consent", json={"policy_version": "0.9", "purpose": "facial skin analysis"}, headers=headers
    )

    resp = await client.post("/analyze", json={"image_base64": "aGVsbG8="}, headers=headers)
    assert resp.status_code == 403

    # Re-consenting to the current required version fixes it.
    await client.post(
        "/consent", json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "facial skin analysis"},
        headers=headers,
    )
    resp2 = await client.post("/analyze", json={"image_base64": "aGVsbG8="}, headers=headers)
    assert resp2.status_code == 422


async def test_withdrawn_consent_denies_analyze(client):
    token = await _signup_and_login(client, "withdrawtest@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/consent", json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "facial skin analysis"},
        headers=headers,
    )
    allowed = await client.post("/analyze", json={"image_base64": "aGVsbG8="}, headers=headers)
    assert allowed.status_code == 422  # past the consent gate

    withdraw = await client.post("/consent/withdraw", json={}, headers=headers)
    assert withdraw.status_code == 200

    denied = await client.post("/analyze", json={"image_base64": "aGVsbG8="}, headers=headers)
    assert denied.status_code == 403


async def test_consent_history_is_append_only(client, db_pool):
    token = await _signup_and_login(client, "historytest@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/consent", json={"policy_version": "0.9", "purpose": "v1"}, headers=headers)
    await client.post("/consent", json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "v2"}, headers=headers)

    rows = await db_pool.fetch(
        "SELECT policy_version, purpose FROM consent_events "
        "WHERE user_id = (SELECT id FROM users WHERE email = $1) ORDER BY granted_at",
        "historytest@test.com",
    )
    # Both grants exist as separate rows -- the older one was never
    # overwritten or deleted by the newer one.
    assert len(rows) == 2
    assert rows[0]["policy_version"] == "0.9"
    assert rows[1]["policy_version"] == REQUIRED_POLICY_VERSION
