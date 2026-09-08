"""Phase 10: per-metric confidence formulas, tested directly with
controlled/synthetic CaptureAssessment inputs -- deterministic, no
real photo or MediaPipe pass needed. Covers: a good-capture case per
metric, a poor-capture-conditions case specific to what actually
corrupts that metric, and a boundary case at the abstain/borderline
threshold.
"""
from app.cv.capture_assessment import CaptureAssessment, QualityStatus
from app.cv.metric_confidence import (
    CONFIDENCE_FUNCTIONS,
    ABSTAIN_THRESHOLD,
    BORDERLINE_THRESHOLD,
    evenness_confidence,
    redness_confidence,
    oiliness_confidence,
    texture_confidence,
    under_eye_darkness_confidence,
    puffiness_confidence,
    feature_definition_confidence,
    symmetry_confidence,
)


def _capture(**overrides) -> CaptureAssessment:
    defaults = dict(
        quality_status=QualityStatus.PASS,
        overall_quality=0.9,
        yaw=0.0, pitch=0.0, roll=0.0,
        blur_score=0.9, exposure_score=0.9, lighting_balance=0.9,
        face_size_score=0.9, resolution_score=0.9, occlusion_score=0.9,
        failure_reasons=[],
    )
    defaults.update(overrides)
    return CaptureAssessment(**defaults)


def test_all_8_metrics_have_a_confidence_function():
    expected = {
        "evenness_score", "redness_score", "oiliness_score", "texture_score",
        "under_eye_darkness", "puffiness_score", "feature_definition_score", "symmetry_score",
    }
    assert set(CONFIDENCE_FUNCTIONS.keys()) == expected


def test_good_capture_yields_high_confidence_for_every_metric():
    good = _capture()
    for name, fn in CONFIDENCE_FUNCTIONS.items():
        confidence, reasons = fn(good)
        assert confidence > BORDERLINE_THRESHOLD, f"{name}: {confidence}"
        assert reasons == [], f"{name}: unexpected reasons {reasons} on a good capture"


def test_evenness_abstains_under_poor_lighting_and_exposure():
    poor = _capture(lighting_balance=0.05, exposure_score=0.05)
    confidence, reasons = evenness_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD
    assert "poor_lighting_balance" in reasons
    assert "poor_exposure" in reasons


def test_redness_abstains_under_poor_lighting_and_exposure():
    poor = _capture(lighting_balance=0.05, exposure_score=0.05)
    confidence, reasons = redness_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD


def test_oiliness_abstains_under_poor_exposure():
    poor = _capture(exposure_score=0.02, lighting_balance=0.1)
    confidence, reasons = oiliness_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD
    assert "poor_exposure" in reasons


def test_texture_abstains_under_severe_blur():
    """The documented circular dependency: texture confidence is
    dominated by blur_score, the same signal the capture pipeline
    itself uses for its own sharpness gate."""
    poor = _capture(blur_score=0.02, resolution_score=0.2)
    confidence, reasons = texture_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD
    assert "excessive_blur" in reasons


def test_under_eye_darkness_abstains_under_poor_lighting_and_pose():
    poor = _capture(lighting_balance=0.05, exposure_score=0.05, yaw=40.0, pitch=30.0)
    confidence, reasons = under_eye_darkness_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD


def test_puffiness_abstains_under_poor_lighting_and_pose():
    poor = _capture(lighting_balance=0.02, yaw=40.0, pitch=35.0, resolution_score=0.1)
    confidence, reasons = puffiness_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD


def test_feature_definition_abstains_under_excessive_pose():
    """Strongly pose-sensitive: a real head turn fakes jaw-angle
    differences far more than actual anatomy would."""
    poor = _capture(yaw=40.0, pitch=0.0, roll=0.0, resolution_score=0.2)
    confidence, reasons = feature_definition_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD
    assert "excessive_head_pose" in reasons


def test_symmetry_abstains_under_excessive_pose():
    """The single most pose-sensitive metric -- any yaw or roll
    directly fakes asymmetry."""
    poor = _capture(yaw=35.0, roll=15.0, blur_score=0.3)
    confidence, reasons = symmetry_confidence(poor)
    assert confidence < ABSTAIN_THRESHOLD
    assert "excessive_head_pose" in reasons


def test_symmetry_confidence_at_the_pose_boundary():
    """Boundary test: symmetry weights pose at 0.85 with a 45-degree
    normalization reference. A pose right at that 45-degree reference
    (pose_severity == 1.0) drives the pose term to zero, leaving only
    the 0.15*blur_score component -- confidence should land close to
    0.15*blur_score, not somewhere arbitrary."""
    at_reference = _capture(yaw=45.0, pitch=0.0, roll=0.0, blur_score=0.9)
    confidence, reasons = symmetry_confidence(at_reference)
    assert abs(confidence - (0.15 * 0.9)) < 0.01
    assert "excessive_head_pose" in reasons


def test_undetermined_head_pose_is_treated_as_maximum_severity_not_zero():
    """A pose that couldn't be solved must never look like a perfect
    frontal capture -- confidence for pose-sensitive metrics should be
    at its floor, not artificially high."""
    undetermined = _capture(yaw=0.0, pitch=0.0, roll=0.0, failure_reasons=["head_pose_undetermined"])
    confidence, reasons = symmetry_confidence(undetermined)
    assert confidence < ABSTAIN_THRESHOLD
    assert "excessive_head_pose" in reasons


def test_confidence_never_exceeds_valid_0_to_1_range():
    extreme = _capture(
        yaw=999.0, pitch=999.0, roll=999.0,
        blur_score=0.0, exposure_score=0.0, lighting_balance=0.0,
        face_size_score=0.0, resolution_score=0.0,
    )
    for name, fn in CONFIDENCE_FUNCTIONS.items():
        confidence, _ = fn(extreme)
        assert 0.0 <= confidence <= 1.0, f"{name}: {confidence} out of range"
