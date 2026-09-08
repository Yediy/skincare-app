"""
Per-metric confidence formulas (Phase 10) -- reflecting the specific
corrupting factors documented in CV_VALIDATION_LIMITATIONS.md for each
of the 8 metrics, not a single blanket capture_quality float applied
uniformly to all of them (the old behavior).

Every formula returns (confidence, uncertainty_reasons). A reason is
only included when the underlying factor is actually degraded enough
to plausibly explain reduced confidence, matching what would really
corrupt that specific measurement -- not a decorative label.
"""
from typing import List, Tuple

from app.cv.capture_assessment import CaptureAssessment

# Below this, a metric abstains (value withheld) rather than being
# presented with a false sense of confidence. Reachable in practice --
# proven directly by tests using deliberately degraded captures, not
# just theoretically defined.
ABSTAIN_THRESHOLD = 0.35
BORDERLINE_THRESHOLD = 0.60

REASON_POOR_LIGHTING_BALANCE = "poor_lighting_balance"
REASON_POOR_EXPOSURE = "poor_exposure"
REASON_EXCESSIVE_BLUR = "excessive_blur"
REASON_LOW_RESOLUTION = "low_resolution"
REASON_EXCESSIVE_POSE = "excessive_head_pose"
REASON_POSE_UNDETERMINED = "head_pose_undetermined"


def _pose_severity(capture: CaptureAssessment) -> float:
    """0.0 (no pose problem) to 1.0 (worst case), normalized against a
    45-degree reference on the largest of yaw/pitch/roll. A pose that
    couldn't be solved at all is treated as maximum severity, not as
    a suspiciously perfect 0 -- solve failure must never look
    confident."""
    if "head_pose_undetermined" in capture.failure_reasons:
        return 1.0
    max_angle = max(abs(capture.yaw), abs(capture.pitch), abs(capture.roll))
    return max(0.0, min(1.0, max_angle / 45.0))


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def evenness_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Corrupted by: lighting uniformity, exposure, region coverage
    (approximated here by face_size_score)."""
    confidence = 0.40 * capture.lighting_balance + 0.35 * capture.exposure_score + 0.25 * capture.face_size_score
    reasons = []
    if capture.lighting_balance < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_LIGHTING_BALANCE)
    if capture.exposure_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_EXPOSURE)
    return _clip01(confidence), reasons


def redness_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Corrupted by: lighting color temperature / white balance
    (approximated by lighting_balance -- the closest available proxy;
    this pipeline has no real color-temperature measurement), exposure."""
    confidence = 0.5 * capture.lighting_balance + 0.5 * capture.exposure_score
    reasons = []
    if capture.lighting_balance < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_LIGHTING_BALANCE)
    if capture.exposure_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_EXPOSURE)
    return _clip01(confidence), reasons


def oiliness_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Corrupted by: flash/specular lighting and exposure (the
    highlight-ratio technique this metric uses is directly sensitive
    to over/under-exposure), lighting uniformity."""
    confidence = 0.6 * capture.exposure_score + 0.4 * capture.lighting_balance
    reasons = []
    if capture.exposure_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_EXPOSURE)
    if capture.lighting_balance < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_LIGHTING_BALANCE)
    return _clip01(confidence), reasons


def texture_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Dominant corrupting factor: blur. This metric and the capture
    pipeline's own blur_score use the same Laplacian-variance
    technique on overlapping regions -- a known, explicitly documented
    circular dependency (CV_VALIDATION_LIMITATIONS.md), not solved
    here, only weighted honestly. Also corrupted by resolution."""
    confidence = 0.70 * capture.blur_score + 0.30 * capture.resolution_score
    reasons = []
    if capture.blur_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_EXCESSIVE_BLUR)
    if capture.resolution_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_LOW_RESOLUTION)
    return _clip01(confidence), reasons


def under_eye_darkness_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Corrupted by: lighting direction/uniformity, exposure, and pose
    (under-eye visibility changes with pitch especially)."""
    pose_factor = 1.0 - 0.6 * _pose_severity(capture)
    confidence = 0.35 * capture.lighting_balance + 0.35 * capture.exposure_score + 0.30 * pose_factor
    reasons = []
    if capture.lighting_balance < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_LIGHTING_BALANCE)
    if capture.exposure_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_EXPOSURE)
    if _pose_severity(capture) > 0.33:
        reasons.append(REASON_EXCESSIVE_POSE)
    return _clip01(confidence), reasons


def puffiness_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Corrupted by: lighting, pose, and resolution (the profile-
    curvature heuristic needs enough pixels to find a real gradient)."""
    pose_factor = 1.0 - 0.6 * _pose_severity(capture)
    confidence = 0.30 * capture.lighting_balance + 0.40 * pose_factor + 0.30 * capture.resolution_score
    reasons = []
    if capture.lighting_balance < BORDERLINE_THRESHOLD:
        reasons.append(REASON_POOR_LIGHTING_BALANCE)
    if _pose_severity(capture) > 0.33:
        reasons.append(REASON_EXCESSIVE_POSE)
    if capture.resolution_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_LOW_RESOLUTION)
    return _clip01(confidence), reasons


def feature_definition_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """Strongly corrupted by pose: a real head turn changes apparent
    jaw angle far more than any actual anatomical difference would.
    Also corrupted by the resolution/camera-distance proxy."""
    pose_factor = 1.0 - _pose_severity(capture)
    confidence = 0.75 * pose_factor + 0.25 * capture.resolution_score
    reasons = []
    if _pose_severity(capture) > 0.20:
        reasons.append(REASON_EXCESSIVE_POSE)
    if capture.resolution_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_LOW_RESOLUTION)
    return _clip01(confidence), reasons


def symmetry_confidence(capture: CaptureAssessment) -> Tuple[float, List[str]]:
    """The single most pose-sensitive metric in the pipeline: any yaw
    or roll directly fakes asymmetry. Landmark quality (approximated
    by blur_score) is the secondary factor."""
    pose_factor = 1.0 - _pose_severity(capture)
    confidence = 0.85 * pose_factor + 0.15 * capture.blur_score
    reasons = []
    if _pose_severity(capture) > 0.15:
        reasons.append(REASON_EXCESSIVE_POSE)
    if capture.blur_score < BORDERLINE_THRESHOLD:
        reasons.append(REASON_EXCESSIVE_BLUR)
    return _clip01(confidence), reasons


# metric_key -> confidence function, keyed identically to
# app/cv/metric_extractors.py's compute_all_metric_results() output
# and app/domain/priorities.py's PriorityDef.metric_key.
CONFIDENCE_FUNCTIONS = {
    "evenness_score": evenness_confidence,
    "redness_score": redness_confidence,
    "oiliness_score": oiliness_confidence,
    "texture_score": texture_confidence,
    "under_eye_darkness": under_eye_darkness_confidence,
    "puffiness_score": puffiness_confidence,
    "feature_definition_score": feature_definition_confidence,
    "symmetry_score": symmetry_confidence,
}
