"""Phase 7: structured CaptureAssessment, replacing the single blended
float. Uses a real photo (vendored test fixture, already used
elsewhere this session) for real landmark detection, then constructs
deliberately degraded variants of the *same* image (blur, darkness) to
exercise the FAIL/BORDERLINE paths deterministically -- the face
location/landmarks don't move when only blur/brightness change, so
reusing one real `detection` across variants is a reasonable,
practical shortcut rather than requiring a differently-lit real photo
for every scenario.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.cv.capture_assessment import CaptureAssessor, QualityStatus
from app.cv.face_landmarks import FaceLandmarkExtractor

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


@pytest.fixture(scope="module")
def real_detection():
    extractor = FaceLandmarkExtractor()
    image_bgr = cv2.imread(str(GRACE_HOPPER_JPG))
    detection = extractor.extract(image_bgr)
    assert detection is not None
    return image_bgr, detection


def test_good_capture_produces_well_formed_assessment(real_detection):
    image_bgr, detection = real_detection
    assessment = CaptureAssessor().assess(image_bgr, detection)

    assert assessment.quality_status in (QualityStatus.PASS, QualityStatus.BORDERLINE)
    assert 0.0 <= assessment.overall_quality <= 1.0
    for field in (
        "blur_score", "exposure_score", "lighting_balance",
        "face_size_score", "resolution_score", "occlusion_score",
    ):
        value = getattr(assessment, field)
        assert 0.0 <= value <= 1.0, f"{field}={value} out of [0,1]"
    assert isinstance(assessment.yaw, float)
    assert assessment.capture_pipeline_version == "1.0"


def test_very_dark_image_degrades_status_and_records_reason(real_detection):
    image_bgr, detection = real_detection
    dark_image = (image_bgr.astype(np.float32) * 0.06).astype(np.uint8)

    assessment = CaptureAssessor().assess(dark_image, detection)

    assert assessment.quality_status in (QualityStatus.BORDERLINE, QualityStatus.FAIL)
    assert assessment.exposure_score < 0.5
    assert any("exposure" in reason for reason in assessment.failure_reasons)


def test_heavily_blurred_image_degrades_status_and_records_reason(real_detection):
    image_bgr, detection = real_detection
    blurred = cv2.GaussianBlur(image_bgr, (0, 0), sigmaX=15)

    assessment = CaptureAssessor().assess(blurred, detection)

    assert assessment.quality_status in (QualityStatus.BORDERLINE, QualityStatus.FAIL)
    assert assessment.blur_score < 0.5
    assert any("blur" in reason for reason in assessment.failure_reasons)


def test_to_dict_is_json_serializable(real_detection):
    import json
    image_bgr, detection = real_detection
    assessment = CaptureAssessor().assess(image_bgr, detection)
    serialized = json.dumps(assessment.to_dict())  # must not raise
    assert "quality_status" in serialized
