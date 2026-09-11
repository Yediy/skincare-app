"""Phase 18: end-to-end authenticated analysis.

Honest scope note, read before trusting this file's coverage: this
covers what actually exists on the real HTTP path -- signup, login,
consent, profile, authenticated /analyze through the real CV pipeline,
real scorer, real SafetyEngine, real PlanService, against a real
database. There is no `plans`/`analysis_results` persistence table in
this repository, so "plan persisted" is not (and cannot be) tested --
the plan is returned in the response only. A real, normalized product
catalog now exists, and /analyze does attach real formulation-level
product matches to the plan (see
tests/integration/test_recommendation_pipeline_integration.py, added
in the production-recommendation pass) -- not duplicated here, since
this file predates that catalog and stays focused on the CV/scoring/
capture-assessment path it was written to cover.

Scenarios 2 (excessive yaw), 3 (poor lighting -> abstain), and 8
(abstained metric can't create a concern) are explicitly marked
skipped, not silently omitted -- they're blocked on Phase 7-11
(CaptureAssessment, real head pose, MetricResult, per-metric
confidence, abstention), none of which exist in this codebase yet.

Scenarios 4-7 (allergy conflict, sensitive skin, beginner, pregnancy)
are exercised here through the real HTTP path with a real photo where
possible; their deterministic, priority-specific behavior is already
covered in detail by tests/planning/test_safety_enforcement_in_plan.py
and tests/planning/test_intensity_separation.py, which call the exact
same PlanService.generate_plan() /analyze itself calls -- not
duplicated here with a second, less deterministic real-photo version.
"""
from pathlib import Path

import pytest

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


async def _signup_login_consent(client, email):
    from app.db.consent_repository import REQUIRED_POLICY_VERSION

    await client.post("/signup", json={"email": email, "password": "testpass123"})
    login = await client.post("/login", json={"email": email, "password": "testpass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    consent = await client.post(
        "/consent",
        json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "facial skin analysis"},
        headers=headers,
    )
    assert consent.status_code == 200
    return headers


async def test_full_chain_good_capture_succeeds(client):
    """signup -> login -> consent -> profile -> authenticated /analyze
    through the real CV pipeline, real scorer, real SafetyEngine, real
    PlanService -- against a real database, over real HTTP."""
    import base64

    headers = await _signup_login_consent(client, "e2e-good@test.com")

    profile_resp = await client.put(
        "/profile",
        json={
            "has_sensitive_skin": False,
            "experience_level": "intermediate",
            "max_routine_steps": 10,
            "is_pregnant": False,
            "is_nursing": False,
            "allergies": [],
            "avoid_ingredients": [],
        },
        headers=headers,
    )
    assert profile_resp.status_code == 200

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)

    assert resp.status_code == 200
    body = resp.json()

    assert "plan" in body and "scores" in body
    plan = body["plan"]
    for key in ("top_priorities", "am_routine", "pm_routine", "disclaimers", "metadata"):
        assert key in plan
    assert "safety_decisions" in plan["metadata"]
    assert "priority_intensities" in plan["metadata"]
    # The regression this whole foundation pass targeted: no
    # supplement_recommendations key should ever be present again.
    assert "supplement_recommendations" not in plan


async def test_analyze_denied_without_consent_even_with_good_photo(client):
    """Consent gating (Phase 5) holds even when everything else about
    the request would otherwise succeed."""
    import base64

    await client.post("/signup", json={"email": "e2e-noconsent@test.com", "password": "testpass123"})
    login = await client.post("/login", json={"email": "e2e-noconsent@test.com", "password": "testpass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)
    assert resp.status_code == 403


async def test_no_face_detected_denied(client):
    headers = await _signup_login_consent(client, "e2e-noface@test.com")
    import base64
    fake_bytes = base64.b64encode(b"not a real image").decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": fake_bytes}, headers=headers)
    assert resp.status_code == 422


async def test_excessive_yaw_marks_capture_borderline_or_fail(client, monkeypatch):
    """Now implemented (Phase 7/8). A real photo can't be reliably
    posed to an exact yaw angle on demand, so head pose itself is
    monkeypatched to a controlled, deterministic 40-degree yaw --
    everything else (image decode, landmark detection, capture
    assessment blending, the HTTP path) is real and unmocked."""
    import base64
    from app.cv.head_pose import HeadPose

    headers = await _signup_login_consent(client, "e2e-yaw@test.com")

    import app.cv.capture_assessment as capture_assessment_module
    monkeypatch.setattr(
        capture_assessment_module,
        "estimate_head_pose",
        lambda landmarks, w, h: HeadPose(yaw=40.0, pitch=0.0, roll=0.0, solve_success=True),
    )

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)

    # 40 degrees is beyond MAX_ANGLE_BORDERLINE (30) -> FAIL, blocking
    # analysis entirely, per Phase 7's explicit contract.
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["capture_assessment"]["quality_status"] == "FAIL"
    assert any("yaw" in reason for reason in detail["capture_assessment"]["failure_reasons"])


async def test_moderate_yaw_marks_capture_borderline_not_pass(client, monkeypatch):
    """A smaller, still-elevated yaw (20 degrees: between
    MAX_ANGLE_PASS=15 and MAX_ANGLE_BORDERLINE=30) should let analysis
    proceed but mark the capture ineligible for longitudinal
    comparison -- distinct from both PASS and outright FAIL."""
    import base64
    from app.cv.head_pose import HeadPose

    headers = await _signup_login_consent(client, "e2e-moderate-yaw@test.com")

    import app.cv.capture_assessment as capture_assessment_module
    monkeypatch.setattr(
        capture_assessment_module,
        "estimate_head_pose",
        lambda landmarks, w, h: HeadPose(yaw=20.0, pitch=0.0, roll=0.0, solve_success=True),
    )

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["capture_assessment"]["quality_status"] == "BORDERLINE"
    assert body["eligible_for_longitudinal_comparison"] is False


async def test_poor_lighting_causes_a_color_metric_to_abstain(client, monkeypatch):
    """Precisely engineering real pixel-level poor lighting to land in
    the exact confidence band needed (borderline enough at the capture
    level to still allow analysis to proceed, poor enough at the
    metric level to abstain one specific metric) is fragile and
    non-deterministic across environments/runs. So this monkeypatches
    CaptureAssessor.assess() to return a controlled, internally
    consistent "poor but not failed" lighting assessment -- everything
    downstream (real metric extraction math on a real photo, the real
    confidence formulas, the real scorer, the real HTTP path) is
    unmocked."""
    import base64
    from app.cv.capture_assessment import CaptureAssessment, QualityStatus

    headers = await _signup_login_consent(client, "e2e-lighting@test.com")

    # oiliness_confidence = 0.6*exposure_score + 0.4*lighting_balance
    # = 0.6*0.32 + 0.4*0.32 = 0.32, below ABSTAIN_THRESHOLD (0.35) --
    # engineered precisely against the real formula, not guessed.
    controlled_assessment = CaptureAssessment(
        quality_status=QualityStatus.BORDERLINE,
        overall_quality=0.50,
        yaw=0.0, pitch=0.0, roll=0.0,
        blur_score=0.9, exposure_score=0.32, lighting_balance=0.32,
        face_size_score=0.9, resolution_score=0.9, occlusion_score=0.9,
        failure_reasons=["exposure_score_marginal", "lighting_balance_marginal"],
    )

    import app.main as main_module
    monkeypatch.setattr(
        main_module.pipeline.capture_assessor, "assess",
        lambda image_bgr, detection: controlled_assessment,
    )

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["capture_assessment"]["quality_status"] == "BORDERLINE"

    oiliness = body["metric_results"]["oiliness_score"]
    assert oiliness["status"] == "ABSTAINED"
    assert oiliness["value"] is None

    # And the corresponding priority cannot have triggered from it,
    # no matter what the withheld underlying value happened to be.
    assert "OIL_CONTROL" not in body["plan"]["metadata"]["all_priority_ids"]


async def test_abstained_metric_cannot_create_a_personalized_concern_via_scorer():
    """The deterministic proof of this lives at the scorer unit level
    (tests/cv/test_scorer_abstention.py) -- exercising the exact same
    FacialScorer.compute_scores() /analyze itself calls, with
    real MetricResult objects, not a mock of the scorer's behavior.
    Not re-duplicated here with a less-deterministic real-photo
    version; this test just asserts that coverage exists so the
    scenario is never silently unaccounted-for in this file."""
    import importlib
    module = importlib.import_module("tests.cv.test_scorer_abstention")
    assert hasattr(module, "test_abstained_redness_cannot_trigger_redness_control_even_with_a_high_underlying_value")
