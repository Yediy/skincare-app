from dataclasses import dataclass
from typing import List, Tuple, Optional
import logging
import numpy as np

logger = logging.getLogger(__name__)

LANDMARK_REGIONS = {
    "forehead": [10, 108, 151, 337, 336, 296, 334, 293, 300, 69, 108, 151],
    "left_cheek": [123, 50, 205, 206, 92, 165, 167],
    "right_cheek": [352, 280, 425, 426, 322, 391, 393],
    "nose": [4, 5, 195, 197, 6, 168],
    "left_under_eye": [25, 110, 24, 23, 22, 26, 112, 243],
    "right_under_eye": [255, 339, 254, 253, 252, 256, 341, 463],
    "chin": [152, 148, 176, 149, 150, 136, 172],
    "left_jaw": [58, 172, 136, 150, 149, 176],
    "right_jaw": [288, 397, 365, 379, 378, 400],
    "t_zone": [8, 9, 6, 197, 195, 5, 4],
}

SYMMETRY_PAIRS = [(33, 263), (133, 362), (61, 291), (50, 280), (58, 288)]

FACE_OVAL_INDICES = [
    10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365,
    379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93,
    234, 127, 162, 21, 54, 103, 67, 109
]

JAWLINE_CONTOUR_LEFT = [127, 234, 132, 58, 172, 136, 150, 149, 176, 148, 152]
JAWLINE_CONTOUR_RIGHT = [356, 454, 361, 288, 397, 365, 379, 378, 400, 377, 152]


@dataclass
class FaceDetectionResult:
    landmarks: np.ndarray
    image_width: int
    image_height: int
    detection_confidence: float
    face_bbox: Tuple[int, int, int, int]


class FaceLandmarkExtractor:
    def __init__(self, min_detection_confidence: float = 0.6):
        import mediapipe as mp
        self.mp_face_mesh = mp.solutions.face_mesh
        self.detector = self.mp_face_mesh.FaceMesh(
            static_image_mode=True,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=min_detection_confidence
        )

    def extract(self, image_bgr: np.ndarray) -> Optional[FaceDetectionResult]:
        import cv2
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        h, w = image_bgr.shape[:2]

        results = self.detector.process(image_rgb)

        if not results.multi_face_landmarks:
            logger.warning("No face detected in image")
            return None

        face_landmarks = results.multi_face_landmarks[0]
        landmarks = np.array([[lm.x, lm.y, lm.z] for lm in face_landmarks.landmark])

        xs = landmarks[:, 0] * w
        ys = landmarks[:, 1] * h
        bbox = (int(xs.min()), int(ys.min()), int(xs.max() - xs.min()), int(ys.max() - ys.min()))

        confidence = self._estimate_confidence(landmarks)

        return FaceDetectionResult(
            landmarks=landmarks, image_width=w, image_height=h,
            detection_confidence=confidence, face_bbox=bbox
        )

    def _estimate_confidence(self, landmarks: np.ndarray) -> float:
        out_of_bounds = np.sum((landmarks[:, :2] < 0) | (landmarks[:, :2] > 1))
        total_coords = landmarks.shape[0] * 2
        return max(0.0, 1.0 - (out_of_bounds / total_coords) * 5)

    def get_region_pixels(self, image_bgr: np.ndarray, detection: FaceDetectionResult, region_name: str):
        import cv2
        if region_name not in LANDMARK_REGIONS:
            raise ValueError(f"Unknown region: {region_name}")

        indices = LANDMARK_REGIONS[region_name]
        h, w = detection.image_height, detection.image_width

        points = detection.landmarks[indices, :2]
        points_px = (points * [w, h]).astype(np.int32)

        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillConvexPoly(mask, cv2.convexHull(points_px), 255)

        region = cv2.bitwise_and(image_bgr, image_bgr, mask=mask)
        x, y, rw, rh = cv2.boundingRect(points_px)

        if rw == 0 or rh == 0:
            return None

        cropped = region[y:y + rh, x:x + rw]
        cropped_mask = mask[y:y + rh, x:x + rw]

        return cropped, cropped_mask
