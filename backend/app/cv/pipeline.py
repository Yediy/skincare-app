import cv2
import numpy as np
import logging
from typing import Dict, Any

from app.cv.capture_assessment import QualityStatus

logger = logging.getLogger(__name__)


class NoFaceDetectedError(Exception):
    pass


class CaptureQualityFailedError(Exception):
    """Raised when CaptureAssessment.quality_status == FAIL. Per Phase
    7's explicit behavior contract: a FAILed capture must not proceed
    to personalized measurement at all -- unlike BORDERLINE, which
    still computes metrics but is flagged ineligible for longitudinal
    comparison."""

    def __init__(self, capture_assessment):
        self.capture_assessment = capture_assessment
        super().__init__(
            f"Capture quality FAILED (overall_quality={capture_assessment.overall_quality:.2f}, "
            f"reasons={capture_assessment.failure_reasons})"
        )


class FacialAnalysisPipeline:
    def __init__(self):
        from app.cv.face_landmarks import FaceLandmarkExtractor
        from app.cv.metric_extractors import SkinMetricExtractor
        from app.cv.capture_assessment import CaptureAssessor

        self.landmark_extractor = FaceLandmarkExtractor()
        self.metric_extractor = SkinMetricExtractor(self.landmark_extractor)
        self.capture_assessor = CaptureAssessor()

    def analyze(self, image_bytes: bytes) -> Dict[str, Any]:
        image_bgr = self._decode_image(image_bytes)

        detection = self.landmark_extractor.extract(image_bgr)
        if detection is None:
            raise NoFaceDetectedError("No face detected in the uploaded image")

        capture_assessment = self.capture_assessor.assess(image_bgr, detection)
        if capture_assessment.quality_status == QualityStatus.FAIL:
            raise CaptureQualityFailedError(capture_assessment)

        metric_results = self.metric_extractor.compute_all_metric_results(
            image_bgr, detection, capture_assessment
        )

        logger.info(
            f"Analysis complete: status={capture_assessment.quality_status.value}, "
            f"overall_quality={capture_assessment.overall_quality:.2f}, "
            f"yaw={capture_assessment.yaw:.1f} pitch={capture_assessment.pitch:.1f} roll={capture_assessment.roll:.1f}"
        )

        return {
            "metric_results": metric_results,
            "capture_assessment": capture_assessment,
            # BORDERLINE captures still produce metrics (Phase 7's
            # "limited feedback"), but must never be treated as a
            # trustworthy baseline for future before/after comparison.
            "eligible_for_longitudinal_comparison": capture_assessment.quality_status == QualityStatus.PASS,
        }

    def _decode_image(self, image_bytes: bytes) -> np.ndarray:
        nparr = np.frombuffer(image_bytes, np.uint8)
        image_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image_bgr is None:
            raise ValueError("Could not decode image bytes")

        image_bgr = self._correct_orientation(image_bytes, image_bgr)

        h, w = image_bgr.shape[:2]
        max_dim = 1600
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            image_bgr = cv2.resize(image_bgr, (int(w * scale), int(h * scale)))

        return image_bgr

    def _correct_orientation(self, original_bytes: bytes, image_bgr: np.ndarray) -> np.ndarray:
        try:
            from PIL import Image, ExifTags
            import io

            pil_img = Image.open(io.BytesIO(original_bytes))
            exif = pil_img._getexif()
            if exif is None:
                return image_bgr

            orientation_key = next((k for k, v in ExifTags.TAGS.items() if v == "Orientation"), None)
            if orientation_key is None or orientation_key not in exif:
                return image_bgr

            orientation = exif[orientation_key]
            rotations = {3: cv2.ROTATE_180, 6: cv2.ROTATE_90_CLOCKWISE, 8: cv2.ROTATE_90_COUNTERCLOCKWISE}
            if orientation in rotations:
                image_bgr = cv2.rotate(image_bgr, rotations[orientation])

        except Exception as e:
            logger.debug(f"EXIF orientation correction skipped: {e}")

        return image_bgr
