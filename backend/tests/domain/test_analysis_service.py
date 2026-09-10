"""Direct tests of app.domain.analysis_service.perform_analysis --
called with no HTTP involved at all (no `client`/ASGI transport), to
actually prove Phase 14's claim: this function doesn't depend on
FastAPI/HTTP, so a future background worker could call it exactly like
this. `/analyze`'s own behavior is still covered end-to-end by
tests/integration/test_end_to_end_analysis.py; this file is the
"same logic, called the other way" proof that file can't provide by
itself.

Reuses the pipeline/scorer/plan_service singletons already constructed
by importing app.main (same MediaPipe-model-loaded-once rationale as
main.py itself) rather than paying to construct a second
FacialAnalysisPipeline.
"""
import base64
import uuid
from pathlib import Path

import pytest

from app.domain.analysis_service import (
    AnalysisRequest,
    AnalysisResult,
    ConsentRequiredError,
    InvalidImageError,
    perform_analysis,
)
from app.domain.entitlement import FreeTierEntitlementService, UsagePolicyService

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


async def _create_user_with_consent(db_pool, email: str) -> str:
    from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, record_consent

    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    user_id = row["id"]
    await record_consent(db_pool, user_id, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, "testing")
    return str(user_id)


@pytest.fixture
def real_singletons(app_db_pool):
    from app.main import pipeline, plan_service, scorer

    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService())
    return {
        "pipeline": pipeline,
        "scorer": scorer,
        "plan_service": plan_service,
        "usage_policy_service": usage_policy_service,
    }


async def test_perform_analysis_returns_a_result_with_no_http_involved(db_pool, app_db_pool, real_singletons):
    """The direct proof: no `client`, no ASGITransport, no FastAPI
    route -- just the domain function, a DB pool, and the same
    singletons /analyze uses."""
    user_id = await _create_user_with_consent(db_pool, "domain-good@test.com")
    image_base64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()

    result = await perform_analysis(
        app_db_pool,
        AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=str(uuid.uuid4())),
        **real_singletons,
    )

    assert isinstance(result, AnalysisResult)
    assert result.plan is not None
    assert isinstance(result.scores, dict) and len(result.scores) > 0
    assert isinstance(result.capture_assessment, dict)
    assert isinstance(result.eligible_for_longitudinal_comparison, bool)


async def test_perform_analysis_raises_consent_required_without_consent(db_pool, app_db_pool, real_singletons):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", "domain-noconsent@test.com"
    )
    user_id = str(row["id"])
    image_base64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()

    with pytest.raises(ConsentRequiredError) as exc_info:
        await perform_analysis(
            app_db_pool,
            AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=str(uuid.uuid4())),
            **real_singletons,
        )
    from app.db.consent_repository import REQUIRED_POLICY_VERSION

    assert exc_info.value.required_policy_version == REQUIRED_POLICY_VERSION


async def test_perform_analysis_raises_invalid_image_for_bad_base64(db_pool, app_db_pool, real_singletons):
    user_id = await _create_user_with_consent(db_pool, "domain-badb64@test.com")

    with pytest.raises(InvalidImageError):
        await perform_analysis(
            app_db_pool,
            AnalysisRequest(user_id=user_id, image_base64="not-valid-base64!!!", request_id=str(uuid.uuid4())),
            **real_singletons,
        )


async def test_perform_analysis_lets_no_face_detected_propagate(db_pool, app_db_pool, real_singletons):
    """A solid-color image has no detectable face -- NoFaceDetectedError
    (app.cv.pipeline) must propagate unchanged, same as before this
    module existed; this function doesn't translate CV-layer
    exceptions, only adds its own two (ConsentRequiredError,
    InvalidImageError)."""
    from app.cv.pipeline import NoFaceDetectedError
    import io

    from PIL import Image

    user_id = await _create_user_with_consent(db_pool, "domain-noface@test.com")
    blank = Image.new("RGB", (200, 200), color=(128, 128, 128))
    buf = io.BytesIO()
    blank.save(buf, format="JPEG")
    image_base64 = base64.b64encode(buf.getvalue()).decode()

    with pytest.raises(NoFaceDetectedError):
        await perform_analysis(
            app_db_pool,
            AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=str(uuid.uuid4())),
            **real_singletons,
        )


async def test_perform_analysis_releases_reservation_when_no_face_detected(db_pool, app_db_pool, real_singletons):
    """A legitimate failure past the reservation point (no face
    detected) must release the quota slot rather than consuming it --
    the user shouldn't be charged for an attempt that produced nothing
    usable."""
    import io

    from PIL import Image

    user_id = await _create_user_with_consent(db_pool, "domain-noface-release@test.com")
    blank = Image.new("RGB", (200, 200), color=(128, 128, 128))
    buf = io.BytesIO()
    blank.save(buf, format="JPEG")
    image_base64 = base64.b64encode(buf.getvalue()).decode()
    request_id = str(uuid.uuid4())

    from app.cv.pipeline import NoFaceDetectedError

    with pytest.raises(NoFaceDetectedError):
        await perform_analysis(
            app_db_pool,
            AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=request_id),
            **real_singletons,
        )

    row = await db_pool.fetchrow(
        "SELECT status FROM analysis_usage WHERE user_id = $1 AND request_id = $2",
        uuid.UUID(user_id), request_id,
    )
    assert row["status"] == "RELEASED"


async def test_perform_analysis_consumes_reservation_on_success(db_pool, app_db_pool, real_singletons):
    user_id = await _create_user_with_consent(db_pool, "domain-consume@test.com")
    image_base64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    request_id = str(uuid.uuid4())

    await perform_analysis(
        app_db_pool,
        AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=request_id),
        **real_singletons,
    )

    row = await db_pool.fetchrow(
        "SELECT status FROM analysis_usage WHERE user_id = $1 AND request_id = $2",
        uuid.UUID(user_id), request_id,
    )
    assert row["status"] == "CONSUMED"


async def test_perform_analysis_raises_quota_exceeded_once_allowance_is_used(db_pool, app_db_pool):
    from app.domain.entitlement import FreeTierEntitlementService, QuotaExceededError, UsagePolicyService
    from app.main import pipeline, plan_service, scorer

    user_id = await _create_user_with_consent(db_pool, "domain-quota@test.com")
    image_base64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    tight_usage_policy = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=1))
    singletons = {"pipeline": pipeline, "scorer": scorer, "plan_service": plan_service}

    # First call consumes the only slot the tight allowance provides.
    await perform_analysis(
        app_db_pool,
        AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=str(uuid.uuid4())),
        usage_policy_service=tight_usage_policy,
        **singletons,
    )

    with pytest.raises(QuotaExceededError):
        await perform_analysis(
            app_db_pool,
            AnalysisRequest(user_id=user_id, image_base64=image_base64, request_id=str(uuid.uuid4())),
            usage_policy_service=tight_usage_policy,
            **singletons,
        )
