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
from app.domain.analysis_submission_service import (
    MAX_ANALYSIS_BASE64_CHARS,
    AnalysisSubmissionService,
)
from app.domain.entitlement import FreeTierEntitlementService, UsagePolicyService
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"
IMAGE_B64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
NOT_AN_IMAGE_B64 = base64.b64encode(b"definitely-not-a-real-image-just-bytes").decode()


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
    # commit_analysis_result() now requires the request to be PROCESSING
    # (System Integrity Gate V1, section 3) -- queue then mark_processing()
    # first, same as the real submission+execute() flow.
    await analysis_repository.mark_queued(
        app_db_pool, user_id, req["id"],
        image_object_key="ephemeral-analysis/test/fixture", image_expires_at=None,
    )
    await analysis_repository.mark_processing(app_db_pool, user_id, req["id"])
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


async def test_get_measurements_response_excludes_internal_columns(client, db_pool, app_db_pool):
    """Section 9: the mobile/API contract is exactly metric_name/value/
    confidence/status/uncertainty_reasons/metric_version/
    calibration_version -- id/analysis_request_id/user_id/created_at
    (internal database bookkeeping) must never appear in the response,
    even though get_measurements() already scopes rows to their owner
    via RLS regardless."""
    from app.db import analysis_repository, usage_repository

    headers = await _signup_login_consent(client, "v2-metric-shape@test.com")
    user_row = await db_pool.fetchrow("SELECT id FROM users WHERE email = $1", "v2-metric-shape@test.com")
    user_id = user_row["id"]

    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    reservation = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=5)
    # commit_analysis_result() now requires the request to be PROCESSING
    # (System Integrity Gate V1, section 3) -- queue then mark_processing()
    # first, same as the real submission+execute() flow.
    await analysis_repository.mark_queued(
        app_db_pool, user_id, req["id"],
        image_object_key="ephemeral-analysis/test/fixture", image_expires_at=None,
    )
    await analysis_repository.mark_processing(app_db_pool, user_id, req["id"])
    await analysis_repository.commit_analysis_result(
        app_db_pool, user_id, req["id"],
        capture_assessment={"quality_status": "PASS"},
        scores={"skin_health_score": 0.8},
        plan={"top_priorities": []},
        eligible_for_longitudinal_comparison=True,
        pipeline_version="test-1.0",
        metric_results={
            "evenness_score": {"value": 0.72, "confidence": 0.9, "status": "VALID", "uncertainty_reasons": []},
        },
        product_recommendations=[],
        usage_reservation_id=reservation.id,
    )

    resp = await client.get(f"/api/v2/analyses/{req['id']}", headers=headers)
    assert resp.status_code == 200
    metric = resp.json()["metric_results"][0]

    assert set(metric.keys()) == {
        "metric_name", "value", "confidence", "status", "uncertainty_reasons",
        "metric_version", "calibration_version",
    }
    assert "id" not in metric
    assert "analysis_request_id" not in metric
    assert "user_id" not in metric
    assert "created_at" not in metric


async def test_get_completed_analysis_product_recommendations_projection(client, db_pool, app_db_pool):
    """Mobile V1 Phase C1: the product_recommendations contract is
    exactly plan_step_key/product_id/formulation_id/brand/product_name/
    safety_status/reason_codes/restrictions/rules_version/
    verification_date/rank_position -- id/user_id/analysis_request_id/
    created_at (internal database bookkeeping) must never appear, even
    though RLS already scopes this row to its owner regardless. Also
    proves the documented fields actually round-trip correctly through
    ProductRecommendationOut."""
    from app.db import analysis_repository, usage_repository

    headers = await _signup_login_consent(client, "v2-product-recs@test.com")
    user_row = await db_pool.fetchrow("SELECT id FROM users WHERE email = $1", "v2-product-recs@test.com")
    user_id = user_row["id"]

    unique = uuid.uuid4().hex[:8]
    brand_id = await db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) RETURNING id", f"Brand-{unique}", f"brand-{unique}"
    )
    product_id = await db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, $2, $3, 'moisturizer') RETURNING id",
        brand_id, f"Product-{unique}", f"product-{unique}",
    )
    formulation_id = await db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type, ingredient_data_status) "
        "VALUES ($1, '1', 'manufacturer_disclosure', 'COMPLETE') RETURNING id",
        product_id,
    )

    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    reservation = await usage_repository.reserve(app_db_pool, user_id, str(uuid.uuid4()), "2026-09", allowance=5)
    # commit_analysis_result() now requires the request to be PROCESSING
    # (System Integrity Gate V1, section 3) -- queue then mark_processing()
    # first, same as the real submission+execute() flow.
    await analysis_repository.mark_queued(
        app_db_pool, user_id, req["id"],
        image_object_key="ephemeral-analysis/test/fixture", image_expires_at=None,
    )
    await analysis_repository.mark_processing(app_db_pool, user_id, req["id"])
    await analysis_repository.commit_analysis_result(
        app_db_pool, user_id, req["id"],
        capture_assessment={"quality_status": "PASS"}, scores={"skin_health_score": 0.8}, plan={"top_priorities": []},
        eligible_for_longitudinal_comparison=True, pipeline_version="test-1.0", metric_results={},
        product_recommendations=[{
            "plan_step_key": "AM:1", "product_id": product_id, "formulation_id": formulation_id,
            "rank_position": 1, "safety_status": "SAFE", "reason_codes": ["FRAGRANCE_FREE"], "restrictions": {},
            "rules_version": "1.0", "brand": f"Brand-{unique}", "product_name": f"Product-{unique}",
            "verification_date": None,
        }],
        usage_reservation_id=reservation.id,
    )

    resp = await client.get(f"/api/v2/analyses/{req['id']}", headers=headers)
    assert resp.status_code == 200
    rec = resp.json()["product_recommendations"][0]

    assert set(rec.keys()) == {
        "plan_step_key", "product_id", "formulation_id", "brand", "product_name",
        "safety_status", "reason_codes", "restrictions", "rules_version",
        "verification_date", "rank_position",
    }
    assert "id" not in rec
    assert "user_id" not in rec
    assert "analysis_request_id" not in rec
    assert "created_at" not in rec

    assert rec["plan_step_key"] == "AM:1"
    assert rec["product_id"] == str(product_id)
    assert rec["formulation_id"] == str(formulation_id)
    assert rec["brand"] == f"Brand-{unique}"
    assert rec["product_name"] == f"Product-{unique}"
    assert rec["safety_status"] == "SAFE"
    assert rec["reason_codes"] == ["FRAGRANCE_FREE"]
    assert rec["rules_version"] == "1.0"
    assert rec["verification_date"] is None
    assert rec["rank_position"] == 1


# Section 7: server-side image input limits, exercised through the
# real HTTP path -- the mobile client is not a security boundary.

async def test_post_with_oversized_encoded_payload_is_rejected_with_413(client):
    headers = await _signup_login_consent(client, "v2-oversized-b64@test.com")
    oversized_b64 = "A" * (MAX_ANALYSIS_BASE64_CHARS + 1)

    resp = await client.post(
        "/api/v2/analyses",
        json={"image_base64": oversized_b64, "request_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert resp.status_code == 413
    # Never echoes the oversized payload or its length back to the
    # client, and never leaks a decoder/library exception.
    assert "AAAA" not in resp.text


async def test_post_with_malformed_image_bytes_that_are_valid_base64_is_rejected_with_422(client):
    headers = await _signup_login_consent(client, "v2-notanimage@test.com")

    resp = await client.post(
        "/api/v2/analyses",
        json={"image_base64": NOT_AN_IMAGE_B64, "request_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert resp.status_code == 422
    body = resp.json()
    # Safe, generic detail only -- never a raw Pillow/decoder exception
    # message, and never the submitted bytes themselves.
    assert "definitely-not-a-real-image" not in resp.text
    assert isinstance(body["detail"], str)


async def test_post_rejection_releases_the_usage_reservation(client, db_pool):
    """Section 7G: any rejection after quota reservation must release
    that reservation -- a client whose oversized/malformed payload gets
    rejected must not lose an analysis credit for the attempt."""
    headers = await _signup_login_consent(client, "v2-reservation-released@test.com")
    request_id = str(uuid.uuid4())

    resp = await client.post(
        "/api/v2/analyses",
        json={"image_base64": NOT_AN_IMAGE_B64, "request_id": request_id},
        headers=headers,
    )
    assert resp.status_code == 422

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"

    # The released reservation must not have consumed the user's
    # allowance -- a normal, valid submission right after must still
    # succeed.
    retry = await client.post(
        "/api/v2/analyses",
        json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert retry.status_code == 202


# GET /api/v2/analyses (Mobile C3 -- history). No consent needed to
# just *read* history -- consent gates submitting a NEW analysis
# (POST), not looking at ones that already exist -- so these tests use
# a plain signup+login where a scenario needs a second, unrelated user.


async def test_history_requires_authentication(client):
    resp = await client.get("/api/v2/analyses")
    assert resp.status_code == 401


async def test_history_is_empty_for_a_new_user(client):
    headers = await _signup_login_consent(client, "v2-history-empty@test.com")
    resp = await client.get("/api/v2/analyses", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


async def test_history_only_returns_the_authenticated_users_own_analyses(client):
    """The exact ownership-isolation property BLOCKER 1 exists to
    guarantee, verified again here for the new list route specifically
    -- a list endpoint is a fresh surface an IDOR could hide in even
    when the single-record GET is provably safe."""
    owner_headers = await _signup_login_consent(client, "v2-history-owner@test.com")
    await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=owner_headers,
    )
    await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=owner_headers,
    )

    other_headers = await _signup_login_consent(client, "v2-history-other@test.com")
    await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=other_headers,
    )

    owner_resp = await client.get("/api/v2/analyses", headers=owner_headers)
    other_resp = await client.get("/api/v2/analyses", headers=other_headers)

    assert len(owner_resp.json()["items"]) == 2
    assert len(other_resp.json()["items"]) == 1
    owner_ids = {item["analysis_id"] for item in owner_resp.json()["items"]}
    other_ids = {item["analysis_id"] for item in other_resp.json()["items"]}
    assert owner_ids.isdisjoint(other_ids)


async def test_history_orders_newest_first(client):
    headers = await _signup_login_consent(client, "v2-history-order@test.com")
    first = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=headers,
    )
    second = await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=headers,
    )

    resp = await client.get("/api/v2/analyses", headers=headers)
    ids = [item["analysis_id"] for item in resp.json()["items"]]
    assert ids == [second.json()["analysis_id"], first.json()["analysis_id"]]


async def test_history_pagination_covers_every_row_exactly_once_no_duplicates_no_gaps(client, db_pool, app_db_pool):
    """Rows are created directly via the repository (bypassing usage
    quota/POST) since this test needs more rows than the free-tier
    monthly allowance permits, and quota enforcement is irrelevant to
    the pagination logic under test here."""
    from app.db import analysis_repository

    headers = await _signup_login_consent(client, "v2-history-paginate@test.com")
    user_row = await db_pool.fetchrow("SELECT id FROM users WHERE email = $1", "v2-history-paginate@test.com")
    user_id = user_row["id"]

    submitted_ids = []
    for _ in range(5):
        req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
        submitted_ids.append(str(req["id"]))

    collected: list[str] = []
    cursor = None
    pages_fetched = 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/api/v2/analyses", params=params, headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        collected.extend(item["analysis_id"] for item in body["items"])
        pages_fetched += 1
        assert pages_fetched <= 10, "pagination did not terminate -- likely a cursor bug"
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]

    # Every submitted id appears in the paginated walk exactly once,
    # newest-first overall.
    assert collected == list(reversed(submitted_ids))


async def test_history_invalid_cursor_returns_400(client):
    headers = await _signup_login_consent(client, "v2-history-badcursor@test.com")
    resp = await client.get("/api/v2/analyses", params={"cursor": "not-a-real-cursor!!"}, headers=headers)
    assert resp.status_code == 400


async def test_history_limit_bounds_are_enforced(client):
    headers = await _signup_login_consent(client, "v2-history-limit@test.com")
    too_small = await client.get("/api/v2/analyses", params={"limit": 0}, headers=headers)
    too_large = await client.get("/api/v2/analyses", params={"limit": 51}, headers=headers)
    assert too_small.status_code == 422
    assert too_large.status_code == 422


async def test_history_masks_unsafe_error_codes_same_as_the_single_record_route(client, db_pool, app_db_pool):
    from app.db import analysis_repository

    headers = await _signup_login_consent(client, "v2-history-errorcode@test.com")
    user_row = await db_pool.fetchrow("SELECT id FROM users WHERE email = $1", "v2-history-errorcode@test.com")
    user_id = user_row["id"]

    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    await analysis_repository.mark_failed(app_db_pool, user_id, req["id"], "INTERNAL_DB_CONSTRAINT_VIOLATION")

    resp = await client.get("/api/v2/analyses", headers=headers)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["status"] == "FAILED"
    assert item["error_code"] == "ANALYSIS_FAILED"
    assert "INTERNAL_DB_CONSTRAINT_VIOLATION" not in resp.text


async def test_history_completed_at_is_null_until_completion(client):
    headers = await _signup_login_consent(client, "v2-history-completedat@test.com")
    await client.post(
        "/api/v2/analyses", json={"image_base64": IMAGE_B64, "request_id": str(uuid.uuid4())}, headers=headers,
    )
    resp = await client.get("/api/v2/analyses", headers=headers)
    item = resp.json()["items"][0]
    assert item["completed_at"] is None
    assert item["created_at"]
