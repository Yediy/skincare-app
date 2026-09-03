import cv2
import numpy as np
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class NoFaceDetectedError(Exception):
    pass


class LowQualityCaptureError(Exception):
    def __init__(self, quality_score: float, threshold: float):
        self.quality_score = quality_score
        self.threshold = threshold
        super().__init__(f"Capture quality {quality_score:.2f} below minimum {threshold:.2f}")


class FacialAnalysisPipeline:
    MIN_ACCEPTABLE_QUALITY = 0.35

    def __init__(self):
        from app.cv.face_landmarks import FaceLandmarkExtractor
        from app.cv.metric_extractors import SkinMetricExtractor
        from app.cv.capture_quality import CaptureQualityAssessor

        self.landmark_extractor = FaceLandmarkExtractor()
        self.metric_extractor = SkinMetricExtractor(self.landmark_extractor)
        self.quality_assessor = CaptureQualityAssessor()

    def analyze(self, image_bytes: bytes) -> Dict[str, Any]:
        image_bgr = self._decode_image(image_bytes)

        detection = self.landmark_extractor.extract(image_bgr)
        if detection is None:
            raise NoFaceDetectedError("No face detected in the uploaded image")

        quality_score = self.quality_assessor.assess(image_bgr, detection)
        if quality_score < self.MIN_ACCEPTABLE_QUALITY:
            raise LowQualityCaptureError(quality_score, self.MIN_ACCEPTABLE_QUALITY)

        metrics = self.metric_extractor.compute_all_metrics(image_bgr, detection)

        logger.info(f"Analysis complete: quality={quality_score:.2f}, metrics={metrics}")

        return {"metrics": metrics, "capture_quality": quality_score}

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
