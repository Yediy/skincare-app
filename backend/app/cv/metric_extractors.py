import cv2
import numpy as np
from typing import Optional, Tuple
import logging

from app.cv.face_landmarks import FACE_OVAL_INDICES, JAWLINE_CONTOUR_LEFT, JAWLINE_CONTOUR_RIGHT

logger = logging.getLogger(__name__)


class SkinMetricExtractor:
    def __init__(self, landmark_extractor):
        self.extractor = landmark_extractor

    def compute_all_metrics(self, image_bgr: np.ndarray, detection) -> dict:
        metrics = {}
        metrics["evenness_score"] = self._compute_evenness(image_bgr, detection)
        metrics["redness_score"] = self._compute_redness(image_bgr, detection)
        metrics["oiliness_score"] = self._compute_oiliness(image_bgr, detection)
        metrics["texture_score"] = self._compute_texture(image_bgr, detection)
        metrics["under_eye_darkness"] = self._compute_under_eye_darkness(image_bgr, detection)
        metrics["puffiness_score"] = self._compute_puffiness(image_bgr, detection)
        metrics["feature_definition_score"] = self._compute_feature_definition(detection)
        metrics["symmetry_score"] = self._compute_symmetry(detection)
        return metrics

    def _compute_evenness(self, image_bgr, detection):
        regions = ["forehead", "left_cheek", "right_cheek"]
        l_values = []

        for region_name in regions:
            result = self.extractor.get_region_pixels(image_bgr, detection, region_name)
            if result is None:
                continue
            region, mask = result
            lab = cv2.cvtColor(region, cv2.COLOR_BGR2LAB)
            l_channel = lab[:, :, 0]
            valid_pixels = l_channel[mask > 0]
            if len(valid_pixels) > 0:
                l_values.extend(valid_pixels.tolist())

        if not l_values:
            return 0.5

        std_dev = np.std(l_values)
        evenness = max(0.0, min(1.0, 1.0 - (std_dev / 40.0)))
        return round(float(evenness), 3)

    def _compute_redness(self, image_bgr, detection):
        regions = ["left_cheek", "right_cheek", "nose"]
        a_values = []

        for region_name in regions:
            result = self.extractor.get_region_pixels(image_bgr, detection, region_name)
            if result is None:
                continue
            region, mask = result
            lab = cv2.cvtColor(region, cv2.COLOR_BGR2LAB)
            a_channel = lab[:, :, 1].astype(np.float32) - 128
            valid_pixels = a_channel[mask > 0]
            if len(valid_pixels) > 0:
                a_values.extend(valid_pixels.tolist())

        if not a_values:
            return 0.3

        mean_a = np.mean(a_values)
        redness = max(0.0, min(1.0, (mean_a - 5) / 30.0))
        return round(float(redness), 3)

    def _compute_oiliness(self, image_bgr, detection):
        result = self.extractor.get_region_pixels(image_bgr, detection, "t_zone")
        if result is None:
            return 0.4

        region, mask = result
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        v_channel = hsv[:, :, 2]
        s_channel = hsv[:, :, 1]

        valid = mask > 0
        if valid.sum() == 0:
            return 0.4

        highlight_mask = (v_channel > 200) & (s_channel < 60) & valid
        highlight_ratio = highlight_mask.sum() / valid.sum()

        oiliness = max(0.0, min(1.0, highlight_ratio * 4))
        return round(float(oiliness), 3)

    def _compute_texture(self, image_bgr, detection):
        regions = ["forehead", "left_cheek", "right_cheek"]
        variances = []

        for region_name in regions:
            result = self.extractor.get_region_pixels(image_bgr, detection, region_name)
            if result is None:
                continue
            region, mask = result
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            masked_laplacian = laplacian[mask > 0]
            if len(masked_laplacian) > 0:
                variances.append(np.var(masked_laplacian))

        if not variances:
            return 0.4

        mean_variance = np.mean(variances)
        texture = max(0.0, min(1.0, mean_variance / 150.0))
        return round(float(texture), 3)

    def _compute_under_eye_darkness(self, image_bgr, detection):
        under_eye_regions = ["left_under_eye", "right_under_eye"]
        cheek_regions = ["left_cheek", "right_cheek"]

        under_eye_l = []
        cheek_l = []

        for region_name in under_eye_regions:
            result = self.extractor.get_region_pixels(image_bgr, detection, region_name)
            if result is None:
                continue
            region, mask = result
            lab = cv2.cvtColor(region, cv2.COLOR_BGR2LAB)
            valid = lab[:, :, 0][mask > 0]
            if len(valid) > 0:
                under_eye_l.extend(valid.tolist())

        for region_name in cheek_regions:
            result = self.extractor.get_region_pixels(image_bgr, detection, region_name)
            if result is None:
                continue
            region, mask = result
            lab = cv2.cvtColor(region, cv2.COLOR_BGR2LAB)
            valid = lab[:, :, 0][mask > 0]
            if len(valid) > 0:
                cheek_l.extend(valid.tolist())

        if not under_eye_l or not cheek_l:
            return 0.4

        under_eye_mean = np.mean(under_eye_l)
        cheek_mean = np.mean(cheek_l)

        darkness_gap = max(0.0, cheek_mean - under_eye_mean)
        darkness = max(0.0, min(1.0, darkness_gap / 25.0))
        return round(float(darkness), 3)

    def _compute_puffiness(self, image_bgr, detection):
        region_scores = []

        for region_name in ["left_under_eye", "right_under_eye"]:
            result = self.extractor.get_region_pixels(image_bgr, detection, region_name)
            if result is None:
                continue

            region, mask = result
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY).astype(np.float32)
            h, _ = gray.shape
            if h < 6:
                continue

            profile = []
            for row in range(h):
                row_mask = mask[row, :] > 0
                if row_mask.sum() > 0:
                    profile.append(np.mean(gray[row, row_mask]))

            if len(profile) < 6:
                continue

            profile = np.array(profile, dtype=np.float32)
            kernel_size = max(3, (len(profile) // 8) | 1)
            smoothed = cv2.blur(profile.reshape(-1, 1), (1, kernel_size)).flatten()

            deriv = np.diff(smoothed)
            sign_changes = int(np.sum(np.diff(np.sign(deriv)) != 0))

            profile_range = float(smoothed.max() - smoothed.min()) + 1e-6
            peak_idx = int(np.argmax(smoothed))
            tail = smoothed[peak_idx:]
            trough_idx = int(np.argmin(tail)) + peak_idx if len(tail) > 0 else peak_idx
            swing = abs(float(smoothed[peak_idx] - smoothed[trough_idx])) / profile_range

            region_score = min(1.0, (sign_changes / 4.0) * 0.5 + swing * 0.5)
            region_scores.append(region_score)

        if not region_scores:
            return 0.4

        puffiness = float(np.mean(region_scores))
        return round(max(0.0, min(1.0, puffiness)), 3)

    def _compute_feature_definition(self, detection):
        landmarks = detection.landmarks

        angle_score = self._jaw_angle_sharpness(landmarks)
        straightness_score = self._jawline_straightness(landmarks)
        projection_score = self._chin_projection(landmarks)

        definition = (angle_score * 0.5 + straightness_score * 0.3 + projection_score * 0.2)
        return round(float(max(0.0, min(1.0, definition))), 3)

    def _jaw_angle_sharpness(self, landmarks):
        angles = []
        for jaw_top_idx, jaw_corner_idx, chin_idx in [(132, 172, 152), (361, 397, 152)]:
            top = landmarks[jaw_top_idx, :2]
            corner = landmarks[jaw_corner_idx, :2]
            chin = landmarks[chin_idx, :2]

            v1 = top - corner
            v2 = chin - corner
            cos_angle = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
            angle_deg = np.degrees(np.arccos(np.clip(cos_angle, -1, 1)))
            angles.append(angle_deg)

        mean_angle = float(np.mean(angles))
        return max(0.0, min(1.0, 1.0 - ((mean_angle - 90) / 70.0)))

    def _jawline_straightness(self, landmarks):
        residual_scores = []
        for contour_indices in [JAWLINE_CONTOUR_LEFT, JAWLINE_CONTOUR_RIGHT]:
            points = landmarks[contour_indices, :2].astype(np.float32)
            if len(points) < 3:
                continue

            vx, vy, x0, y0 = cv2.fitLine(points, cv2.DIST_L2, 0, 0.01, 0.01).flatten()
            direction = np.array([vx, vy])
            origin = np.array([x0, y0])

            residuals = []
            for p in points:
                to_point = p - origin
                projected_length = np.dot(to_point, direction)
                projected_point = origin + projected_length * direction
                residuals.append(float(np.linalg.norm(p - projected_point)))

            jaw_span = float(np.linalg.norm(points[0] - points[-1])) + 1e-6
            normalized_residual = float(np.mean(residuals)) / jaw_span
            residual_scores.append(normalized_residual)

        if not residual_scores:
            return 0.5

        mean_residual = float(np.mean(residual_scores))
        return max(0.0, min(1.0, 1.0 - (mean_residual / 0.08)))

    def _chin_projection(self, landmarks):
        face_plane_z = float(np.mean(landmarks[FACE_OVAL_INDICES, 2]))
        chin_z = float(landmarks[152, 2])
        projection = face_plane_z - chin_z
        return max(0.0, min(1.0, projection / 0.03))

    def _compute_symmetry(self, detection):
        landmarks = detection.landmarks
        midline_x = np.mean(landmarks[[4, 5, 6, 195], 0])

        deviations = []
        for left_idx, right_idx in [(33, 263), (133, 362), (61, 291), (50, 280), (58, 288)]:
            left_point = landmarks[left_idx, :2]
            right_point = landmarks[right_idx, :2]

            left_dist = abs(left_point[0] - midline_x)
            right_dist = abs(right_point[0] - midline_x)
            y_diff = abs(left_point[1] - right_point[1])

            x_asymmetry = abs(left_dist - right_dist) / max(left_dist, right_dist, 1e-6)
            deviations.append(x_asymmetry + y_diff * 2)

        mean_deviation = np.mean(deviations)
        symmetry = max(0.0, min(1.0, 1.0 - mean_deviation * 3))
        return round(float(symmetry), 3)
