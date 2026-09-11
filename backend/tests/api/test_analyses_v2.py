"""POST /api/v2/analyses, GET /api/v2/analyses/{analysis_id} --
end-to-end through the real HTTP path (signup/login/consent, real
auth, real rate limiting, real Postgres), same shape as
tests/integration/test_end_to_end_analysis.py for /analyze. Object
storage is the only faked piece (network calls to a real Cloudflare
R2 endpoint have no place in this test suite) -- swapped in by
monkeypatching app.api.v2.analyses._build_submission_service, the
module's own per-request service factory.
"""
import base64
import uuid
from pathlib import Path

import pytest

import app.api.v2.analyses as analyses_module
from app.config import settings
from app.domain.analysis_submission_service import AnalysisSubmissionService
from app.domain.entitlement import FreeTierEntitlementService, UsagePolicyService
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"
IMAGE_B64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()


class FakeObjectStorage(ObjectStorage):
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put(self, key, data, *, content_type=None):
        self.objects[key] = data

    async def get(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def delete(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        del self.objects[key]

    async def exists(self, key):
        return key in self.objects

    async def create_upload_authorization(self, key, *, content_type=None, expires_in_seconds=900):
        raise NotImplementedError

    async def create_download_authorization(self, key, *, expires_in_seconds=900):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _enable_async_storage_with_fake_backend(monkeypatch, app_db_pool):
    monkeypatch.setattr(settings, "async_image_storage_enabled", True)

    fake_storage = FakeObjectStorage()

    def _fake_build_submission_service():
        return AnalysisSubmissionService(
            app_db_pool,
            usage_policy_service=UsagePolicyService(app_db_pool, FreeTierEntitlementService()),
            image_store=EphemeralAnalysisImageStore(fake_storage),
            job_queue=PostgresJobQueue(app_db_pool),
            async_image_storage_enabled=True,
        )

    monkeypatch.setattr(analyses_module, "_build_submission_service", _fake_build_submission_service)
    return fake_storage


async def _signup_login_consent(client, email):
    from app.db.consent_repository import REQUIRED_POLICY_VERSION

    await client.post("/signup", json={"email": email, "password": "testpass123"})
    login = await client.post("/login", json={"email": email, "password": "testpass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/consent",
        json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "facial skin analysis"},
        headers=headers,
    )
    return headers


async def test_post_returns_202_and_queued_status(client):
    headers = await _signup_login_consent(client, "v2-post@test.com")
    request_id = str(uuid.uuid4())

    resp = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": request_id}, headers=headers,
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["request_id"] == request_id
    assert body["analysis_id"]


async def test_post_missing_request_id_is_rejected(client):
    headers = await _signup_login_consent(client, "v2-norequestid@test.com")
    resp = await client.post("/api/v2/analyses", json={"image_base64": IMAGE_B64}, headers=headers)
    assert resp.status_code == 422  # pydantic validation -- request_id is required, never manufactured


async def test_post_without_consent_is_rejected(client):
    await client.post("/signup", json={"email": "v2-noconsent@test.com", "password": "testpass123"})
    login = await client.post("/login", json={"email": "v2-noconsent@test.com", "password": "testpass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=headers,
    )
    assert resp.status_code == 403


async def test_idempotent_post_returns_same_analysis(client):
    headers = await _signup_login_consent(client, "v2-idempotent@test.com")
    request_id = str(uuid.uuid4())

    first = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": request_id}, headers=headers,
    )
    second = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": request_id}, headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["analysis_id"] == second.json()["analysis_id"]


async def test_get_owner_succeeds(client):
    headers = await _signup_login_consent(client, "v2-get-owner@test.com")
    submit = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=headers,
    )
    analysis_id = submit.json()["analysis_id"]

    resp = await client.get(f"/api/v2/analyses/{analysis_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_id"] == analysis_id
    assert body["status"] in ("QUEUED", "PROCESSING", "COMPLETED", "FAILED")


async def test_get_another_users_analysis_is_denied(client):
    owner_headers = await _signup_login_consent(client, "v2-get-owner2@test.com")
    submit = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=owner_headers,
    )
    analysis_id = submit.json()["analysis_id"]

    other_headers = await _signup_login_consent(client, "v2-get-other@test.com")
    resp = await client.get(f"/api/v2/analyses/{analysis_id}", headers=other_headers)
    assert resp.status_code == 404  # never distinguishes "not yours" from "doesn't exist"


async def test_get_nonexistent_analysis_is_denied_identically(client):
    headers = await _signup_login_consent(client, "v2-get-missing@test.com")
    resp = await client.get(f"/api/v2/analyses/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


async def test_home_region_and_cell_id_are_snapshotted_onto_the_request(client, db_pool):
    headers = await _signup_login_consent(client, "v2-placement@test.com")
    submit = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=headers,
    )
    analysis_id = submit.json()["analysis_id"]

    # db_pool (superuser) bypasses RLS -- app_db_pool would need
    # app.current_user_id set first, same as every other RLS-scoped
    # assertion in this test suite.
    row = await db_pool.fetchrow(
        "SELECT home_region, cell_id FROM analysis_requests WHERE id = $1", uuid.UUID(analysis_id)
    )
    assert row["home_region"] == settings.launch_home_region
    assert row["cell_id"] == settings.launch_cell_id
