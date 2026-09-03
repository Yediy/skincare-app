import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


class CaptureQualityAssessor:
    def assess(self, image_bgr: np.ndarray, detection) -> float:
        scores = {
            "sharpness": self._assess_sharpness(image_bgr),
            "brightness": self._assess_brightness(image_bgr),
            "face_size": self._assess_face_size(image_bgr, detection),
            "detection_confidence": detection.detection_confidence,
        }

        weights = {"sharpness": 0.35, "brightness": 0.2, "face_size": 0.2, "detection_confidence": 0.25}
        combined = sum(scores[k] * weights[k] for k in weights)

        logger.info(f"Capture quality breakdown: {scores} -> combined={combined:.3f}")
        return round(float(combined), 3)

    def _assess_sharpness(self, image_bgr):
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        return max(0.0, min(1.0, laplacian_var / 300.0))

    def _assess_brightness(self, image_bgr):
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        mean_brightness = np.mean(gray)
        if mean_brightness < 60:
            return mean_brightness / 60.0
        elif mean_brightness > 220:
            return max(0.0, 1.0 - (mean_brightness - 220) / 35.0)
        else:
            return 1.0

    def _assess_face_size(self, image_bgr, detection):
        img_area = detection.image_width * detection.image_height
        _, _, fw, fh = detection.face_bbox
        face_area = fw * fh
        ratio = face_area / img_area if img_area > 0 else 0

        if ratio < 0.05:
            return ratio / 0.05
        elif ratio > 0.7:
            return max(0.0, 1.0 - (ratio - 0.7) / 0.3)
        else:
            return 1.0
