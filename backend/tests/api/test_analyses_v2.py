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


async def test_get_completed_analysis_includes_metric_results(client, db_pool, app_db_pool):
    """Mobile V1 Phase B: GET must expose the per-metric VALID/
    BORDERLINE/ABSTAINED breakdown (analysis_measurements), not just
    the aggregate `scores` blob -- an ABSTAINED metric's `value` must
    round-trip as null, never a fabricated number."""
    from app.db import analysis_repository, usage_repository

    headers = await _signup_login_consent(client, "v2-metric-results@test.com")
    user_row = await db_pool.fetchrow("SELECT id FROM users WHERE email = $1", "v2-metric-results@test.com")
    user_id = user_row["id"]

    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    reservation = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=5)
    await analysis_repository.commit_analysis_result(
        app_db_pool, user_id, req["id"],
        capture_assessment={"quality_status": "PASS"},
        scores={"skin_health_score": 0.8},
        plan={"top_priorities": []},
        eligible_for_longitudinal_comparison=True,
        pipeline_version="test-1.0",
        metric_results={
            "evenness_score": {"value": 0.72, "confidence": 0.9, "status": "VALID", "uncertainty_reasons": []},
            "redness_score": {
                "value": None, "confidence": 0.1, "status": "ABSTAINED",
                "uncertainty_reasons": ["excessive_blur"],
            },
        },
        product_recommendations=[],
        usage_reservation_id=reservation.id,
    )

    resp = await client.get(f"/api/v2/analyses/{req['id']}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETED"

    metrics = {m["metric_name"]: m for m in body["metric_results"]}
    assert metrics["evenness_score"]["status"] == "VALID"
    assert metrics["evenness_score"]["value"] == pytest.approx(0.72)
    assert metrics["redness_score"]["status"] == "ABSTAINED"
    assert metrics["redness_score"]["value"] is None
    assert metrics["redness_score"]["uncertainty_reasons"] == ["excessive_blur"]
