"""
Structured capture-quality contract, replacing CaptureQualityAssessor's
single blended float. Incorporates real head pose (app/cv/head_pose.py)
as a first-class gating factor, not an afterthought.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

import cv2
import numpy as np

from app.cv.head_pose import estimate_head_pose

CAPTURE_PIPELINE_VERSION = "1.0"

# Pose thresholds, in degrees. Within MAX_ANGLE_PASS: no penalty.
# Between PASS and BORDERLINE: usable but flagged. Beyond BORDERLINE:
# FAIL outright -- these are not derived from any validated dataset,
# they're a documented, reasonable-sounding starting point (a face
# turned >30 degrees is visibly, unambiguously off-angle to a human
# looking at the same photo). Treat as a tuning knob, not a fixed law.
MAX_ANGLE_PASS = 15.0
MAX_ANGLE_BORDERLINE = 30.0


class QualityStatus(str, Enum):
    PASS = "PASS"
    BORDERLINE = "BORDERLINE"
    FAIL = "FAIL"


@dataclass
class CaptureAssessment:
    quality_status: QualityStatus
    overall_quality: float
    yaw: float
    pitch: float
    roll: float
    blur_score: float
    exposure_score: float
    lighting_balance: float
    face_size_score: float
    resolution_score: float
    occlusion_score: float
    failure_reasons: List[str] = field(default_factory=list)
    capture_pipeline_version: str = CAPTURE_PIPELINE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quality_status": self.quality_status.value,
            "overall_quality": self.overall_quality,
            "yaw": self.yaw,
            "pitch": self.pitch,
            "roll": self.roll,
            "blur_score": self.blur_score,
            "exposure_score": self.exposure_score,
            "lighting_balance": self.lighting_balance,
            "face_size_score": self.face_size_score,
            "resolution_score": self.resolution_score,
            "occlusion_score": self.occlusion_score,
            "failure_reasons": self.failure_reasons,
            "capture_pipeline_version": self.capture_pipeline_version,
        }


class CaptureAssessor:
    # Hard floor: any single factor below this forces FAIL regardless
    # of how the blended overall_quality looks. Borderline floor: below
    # this (for the blend, or any one factor) forces at least BORDERLINE.
    HARD_FLOOR = 0.30
    BORDERLINE_FLOOR = 0.55

    _WEIGHTS = {
        "blur_score": 0.25,
        "exposure_score": 0.20,
        "lighting_balance": 0.15,
        "face_size_score": 0.20,
        "resolution_score": 0.10,
        "occlusion_score": 0.10,
    }

    def assess(self, image_bgr: np.ndarray, detection) -> CaptureAssessment:
        sub_scores = {
            "blur_score": self._assess_blur(image_bgr),
            "exposure_score": self._assess_exposure(image_bgr),
            "lighting_balance": self._assess_lighting_balance(image_bgr, detection),
            "face_size_score": self._assess_face_size(image_bgr, detection),
            "resolution_score": self._assess_resolution(image_bgr),
            # Proxy, not a real occlusion classifier: MediaPipe FaceMesh
            # doesn't expose per-landmark visibility/occlusion for this
            # model, so this reuses the landmark-boundary-consistency
            # confidence already computed during detection (see
            # face_landmarks.py's _estimate_confidence). A hand covering
            # part of the face often does depress this, but that's a
            # side effect, not a design guarantee -- documented plainly
            # rather than presented as validated occlusion detection.
            "occlusion_score": float(detection.detection_confidence),
        }

        overall_quality = sum(sub_scores[k] * self._WEIGHTS[k] for k in self._WEIGHTS)
        overall_quality = round(float(max(0.0, min(1.0, overall_quality))), 3)

        pose = estimate_head_pose(detection.landmarks, detection.image_width, detection.image_height)

        failure_reasons: List[str] = []
        for name, score in sub_scores.items():
            if score < self.HARD_FLOOR:
                failure_reasons.append(f"{name}_too_low")
            elif score < self.BORDERLINE_FLOOR:
                failure_reasons.append(f"{name}_marginal")

        pose_fail = False
        pose_borderline = False
        if not pose.solve_success:
            failure_reasons.append("head_pose_undetermined")
            pose_borderline = True
        else:
            for axis_name, axis_value in (("yaw", pose.yaw), ("pitch", pose.pitch), ("roll", pose.roll)):
                magnitude = abs(axis_value)
                if magnitude > MAX_ANGLE_BORDERLINE:
                    failure_reasons.append(f"excessive_{axis_name}")
                    pose_fail = True
                elif magnitude > MAX_ANGLE_PASS:
                    failure_reasons.append(f"elevated_{axis_name}")
                    pose_borderline = True

        any_hard_floor_breach = any(s < self.HARD_FLOOR for s in sub_scores.values())
        any_borderline_breach = any(s < self.BORDERLINE_FLOOR for s in sub_scores.values())

        if overall_quality < self.HARD_FLOOR or any_hard_floor_breach or pose_fail:
            status = QualityStatus.FAIL
        elif overall_quality < self.BORDERLINE_FLOOR or any_borderline_breach or pose_borderline:
            status = QualityStatus.BORDERLINE
        else:
            status = QualityStatus.PASS

        return CaptureAssessment(
            quality_status=status,
            overall_quality=overall_quality,
            yaw=round(pose.yaw, 2),
            pitch=round(pose.pitch, 2),
            roll=round(pose.roll, 2),
            blur_score=sub_scores["blur_score"],
            exposure_score=sub_scores["exposure_score"],
            lighting_balance=sub_scores["lighting_balance"],
            face_size_score=sub_scores["face_size_score"],
            resolution_score=sub_scores["resolution_score"],
            occlusion_score=sub_scores["occlusion_score"],
            failure_reasons=failure_reasons,
        )

    def _assess_blur(self, image_bgr: np.ndarray) -> float:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        return round(max(0.0, min(1.0, laplacian_var / 300.0)), 3)

    def _assess_exposure(self, image_bgr: np.ndarray) -> float:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        mean_brightness = np.mean(gray)
        if mean_brightness < 60:
            score = mean_brightness / 60.0
        elif mean_brightness > 220:
            score = max(0.0, 1.0 - (mean_brightness - 220) / 35.0)
        else:
            score = 1.0
        return round(float(score), 3)

    def _assess_lighting_balance(self, image_bgr: np.ndarray, detection) -> float:
        """Left/right face-half brightness gap, as a proxy for
        directional-lighting uniformity -- not a real illuminance
        analysis. Strong side lighting corrupts color- and shadow-
        based metrics (evenness, redness, under-eye darkness); see
        CV_VALIDATION_LIMITATIONS.md."""
        x, y, w, h = detection.face_bbox
        img_h, img_w = image_bgr.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(img_w, x + w), min(img_h, y + h)
        if x1 <= x0 or y1 <= y0:
            return 0.5
        face_region = cv2.cvtColor(image_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        mid = face_region.shape[1] // 2
        if mid == 0:
            return 0.5
        left_mean = float(np.mean(face_region[:, :mid]))
        right_mean = float(np.mean(face_region[:, mid:]))
        gap = abs(left_mean - right_mean)
        return round(max(0.0, min(1.0, 1.0 - gap / 100.0)), 3)

    def _assess_face_size(self, image_bgr: np.ndarray, detection) -> float:
        img_area = detection.image_width * detection.image_height
        _, _, fw, fh = detection.face_bbox
        face_area = fw * fh
        ratio = face_area / img_area if img_area > 0 else 0
        if ratio < 0.05:
            score = ratio / 0.05
        elif ratio > 0.7:
            score = max(0.0, 1.0 - (ratio - 0.7) / 0.3)
        else:
            score = 1.0
        return round(float(score), 3)

    def _assess_resolution(self, image_bgr: np.ndarray) -> float:
        """Proxy for whether the image has enough raw pixels for
        fine-grained metrics (texture, landmark-based symmetry) to be
        meaningful. 720px shorter-edge target is a reasonable-sounding
        starting point, not a validated threshold."""
        h, w = image_bgr.shape[:2]
        return round(max(0.0, min(1.0, min(h, w) / 720.0)), 3)
