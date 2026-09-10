"""app.db.analysis_repository -- Part III (analysis lifecycle
persistence) and Part VI, Phase 30 (atomic result commit), through the
real restricted skincare_app role.
"""
import uuid

import pytest

from app.db import analysis_repository, usage_repository


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def _reserve(app_db_pool, user_id, request_id="r1", allowance=5):
    return await usage_repository.reserve(app_db_pool, user_id, request_id, "2026-09", allowance)


async def test_create_request_is_idempotent(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "analysis-req-idempotent@test.com")
    request_id = str(uuid.uuid4())

    first = await analysis_repository.create_request(app_db_pool, user_id, request_id)
    second = await analysis_repository.create_request(app_db_pool, user_id, request_id)

    assert first["id"] == second["id"]
    assert first["status"] == "RECEIVED"

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_requests WHERE request_id = $1", request_id
    )
    assert count == 1


async def test_request_lifecycle_transitions(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "analysis-req-lifecycle@test.com")
    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))

    await analysis_repository.mark_queued(
        app_db_pool, user_id, req["id"], image_object_key="ephemeral-analysis/x/y/z", image_expires_at=None
    )
    queued = await analysis_repository.get_request_by_id(app_db_pool, user_id, req["id"])
    assert queued["status"] == "QUEUED"
    assert queued["queued_at"] is not None
    assert queued["image_object_key"] == "ephemeral-analysis/x/y/z"

    await analysis_repository.mark_processing(app_db_pool, user_id, req["id"])
    processing = await analysis_repository.get_request_by_id(app_db_pool, user_id, req["id"])
    assert processing["status"] == "PROCESSING"
    assert processing["started_at"] is not None


async def test_mark_failed_records_error_code(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "analysis-req-failed@test.com")
    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))

    await analysis_repository.mark_failed(app_db_pool, user_id, req["id"], "NO_FACE_DETECTED")
    row = await analysis_repository.get_request_by_id(app_db_pool, user_id, req["id"])
    assert row["status"] == "FAILED"
    assert row["error_code"] == "NO_FACE_DETECTED"
    assert row["failed_at"] is not None


async def test_get_request_by_request_id(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "analysis-req-by-rid@test.com")
    request_id = str(uuid.uuid4())
    created = await analysis_repository.create_request(app_db_pool, user_id, request_id)

    found = await analysis_repository.get_request_by_request_id(app_db_pool, user_id, request_id)
    assert found["id"] == created["id"]


async def test_get_result_returns_none_before_commit(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "analysis-req-no-result@test.com")
    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    assert await analysis_repository.get_result(app_db_pool, user_id, req["id"]) is None


async def test_commit_analysis_result_persists_everything_atomically(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "analysis-commit@test.com")
    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    reservation = await _reserve(app_db_pool, user_id, request_id=str(uuid.uuid4()))

    metric_results = {
        "redness": {"value": 0.4, "confidence": 0.9, "status": "VALID", "uncertainty_reasons": [], "metric_version": "1.0", "calibration_version": "uncalibrated-1.0"},
        "texture": {"value": None, "confidence": 0.0, "status": "ABSTAINED", "uncertainty_reasons": ["blur"], "metric_version": "1.0", "calibration_version": "uncalibrated-1.0"},
    }
    product_recommendations = [
        {
            "plan_step_key": "AM:1", "product_id": str(uuid.uuid4()), "formulation_id": str(uuid.uuid4()),
            "rank_position": 1, "safety_status": "SAFE", "reason_codes": [], "restrictions": {}, "rules_version": "1.0",
        },
    ]

    # product_id/formulation_id must be real FKs -- seed minimal
    # catalog rows so the FK constraint is satisfiable. Catalog tables
    # are deliberately outside clean_database's TRUNCATE list (see
    # synthetic_catalog's docstring -- catalog data is self-truncated
    # by the fixtures that own it), so a unique suffix is required
    # here to avoid colliding with a row this same test left behind on
    # a prior run.
    unique = uuid.uuid4().hex[:8]
    brand_id = await db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) RETURNING id", f"X-{unique}", f"x-{unique}"
    )
    product_id = await db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, $2, $3, 'moisturizer') RETURNING id",
        brand_id, f"P-{unique}", f"p-{unique}",
    )
    formulation_id = await db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type, ingredient_data_status) "
        "VALUES ($1, '1', 'manufacturer_disclosure', 'COMPLETE') RETURNING id",
        product_id,
    )
    product_recommendations[0]["product_id"] = product_id
    product_recommendations[0]["formulation_id"] = formulation_id

    await analysis_repository.commit_analysis_result(
        app_db_pool, user_id, req["id"],
        capture_assessment={"quality_status": "PASS"},
        scores={"overall": 0.8},
        plan={"am_routine": []},
        eligible_for_longitudinal_comparison=True,
        pipeline_version="1.0",
        metric_results=metric_results,
        product_recommendations=product_recommendations,
        usage_reservation_id=reservation.id,
    )

    result = await analysis_repository.get_result(app_db_pool, user_id, req["id"])
    assert result["scores"]["overall"] == 0.8

    measurements = await db_pool.fetch(
        "SELECT metric_name, value, status FROM analysis_measurements WHERE analysis_request_id = $1", req["id"]
    )
    by_name = {m["metric_name"]: m for m in measurements}
    assert by_name["redness"]["value"] == 0.4
    assert by_name["texture"]["value"] is None  # abstained -- preserved, not discarded
    assert by_name["texture"]["status"] == "ABSTAINED"

    recs = await analysis_repository.get_product_recommendations(app_db_pool, user_id, req["id"])
    assert len(recs) == 1
    assert recs[0]["plan_step_key"] == "AM:1"

    request_row = await analysis_repository.get_request_by_id(app_db_pool, user_id, req["id"])
    assert request_row["status"] == "COMPLETED"
    assert request_row["completed_at"] is not None

    usage_row = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE id = $1", reservation.id)
    assert usage_row["status"] == "CONSUMED"


async def test_commit_analysis_result_is_idempotent_on_retry(db_pool, app_db_pool):
    """A worker retrying commit_analysis_result() for the same
    analysis_request_id (e.g. after a crash right after the first
    commit but before acknowledging the job) must not create duplicate
    rows or re-consume quota a second time."""
    user_id = await _create_user(db_pool, "analysis-commit-retry@test.com")
    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    reservation = await _reserve(app_db_pool, user_id, request_id=str(uuid.uuid4()))

    kwargs = dict(
        capture_assessment={"quality_status": "PASS"},
        scores={"overall": 0.5},
        plan={"am_routine": []},
        eligible_for_longitudinal_comparison=True,
        pipeline_version="1.0",
        metric_results={},
        product_recommendations=[],
        usage_reservation_id=reservation.id,
    )

    await analysis_repository.commit_analysis_result(app_db_pool, user_id, req["id"], **kwargs)
    await analysis_repository.commit_analysis_result(app_db_pool, user_id, req["id"], **kwargs)  # retry

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_results WHERE analysis_request_id = $1", req["id"]
    )
    assert count == 1
