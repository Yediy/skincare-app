"""
Real head-pose (yaw/pitch/roll) estimation via cv2.solvePnP against a
generic 3D face model and 6 stable MediaPipe FaceMesh landmarks.

This is a monocular, uncalibrated-camera estimate: focal length is
approximated from image width (no camera calibration exists for an
arbitrary phone/webcam photo), and the 3D model points are generic
average-face proportions, not the actual subject's measurements. This
is adequate for gating excessive pose and for confidence weighting
(Phase 10) -- it is not precision metrology, and should not be
presented as one.
"""
from dataclasses import dataclass

import cv2
import numpy as np

NOSE_TIP_IDX = 1
CHIN_IDX = 152
LEFT_EYE_CORNER_IDX = 33
RIGHT_EYE_CORNER_IDX = 263
LEFT_MOUTH_CORNER_IDX = 61
RIGHT_MOUTH_CORNER_IDX = 291

# Generic 3D face model (mm), a standard reference set for monocular
# head-pose estimation from a small number of stable landmarks -- not
# derived from or specific to any individual subject.
_MODEL_POINTS = np.array([
    (0.0, 0.0, 0.0),          # Nose tip
    (0.0, -330.0, -65.0),     # Chin
    (-225.0, 170.0, -135.0),  # Left eye, left corner
    (225.0, 170.0, -135.0),   # Right eye, right corner
    (-150.0, -150.0, -125.0), # Left mouth corner
    (150.0, -150.0, -125.0),  # Right mouth corner
], dtype=np.float64)


@dataclass
class HeadPose:
    yaw: float
    pitch: float
    roll: float
    solve_success: bool


def estimate_head_pose(landmarks: np.ndarray, image_width: int, image_height: int) -> HeadPose:
    """
    landmarks: the (N, 3) normalized landmark array from
    FaceLandmarkExtractor (x, y in [0, 1], z relative depth) -- the
    same array already produced by face_landmarks.py, not a new
    detection pass.

    Returns yaw/pitch/roll in degrees. Positive yaw = turned to the
    subject's... (sign convention follows OpenCV's solvePnP/Rodrigues
    output directly, not independently re-derived) right in-frame;
    magnitude, not sign, is what capture-quality gating and confidence
    weighting actually use.
    """
    image_points = np.array([
        landmarks[NOSE_TIP_IDX, :2] * [image_width, image_height],
        landmarks[CHIN_IDX, :2] * [image_width, image_height],
        landmarks[LEFT_EYE_CORNER_IDX, :2] * [image_width, image_height],
        landmarks[RIGHT_EYE_CORNER_IDX, :2] * [image_width, image_height],
        landmarks[LEFT_MOUTH_CORNER_IDX, :2] * [image_width, image_height],
        landmarks[RIGHT_MOUTH_CORNER_IDX, :2] * [image_width, image_height],
    ], dtype=np.float64)

    focal_length = float(image_width)
    center = (image_width / 2.0, image_height / 2.0)
    camera_matrix = np.array([
        [focal_length, 0, center[0]],
        [0, focal_length, center[1]],
        [0, 0, 1],
    ], dtype=np.float64)
    dist_coeffs = np.zeros((4, 1))

    try:
        success, rotation_vector, translation_vector = cv2.solvePnP(
            _MODEL_POINTS, image_points, camera_matrix, dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
    except cv2.error:
        # Degenerate input (e.g. collapsed/duplicate landmark points)
        # can make solvePnP raise a raw C++ assertion instead of
        # returning success=False -- treated identically to a reported
        # failure, not left to propagate as an unhandled exception.
        success = False

    if not success:
        return HeadPose(yaw=0.0, pitch=0.0, roll=0.0, solve_success=False)

    rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
    pose_matrix = cv2.hconcat((rotation_matrix, translation_vector))
    _, _, _, _, _, _, euler_angles = cv2.decomposeProjectionMatrix(pose_matrix)
    pitch, yaw, roll = (float(a) for a in euler_angles.flatten())

    # decomposeProjectionMatrix can return pitch/roll outside (-90, 90)
    # depending on orientation -- normalize the standard wrap case
    # rather than presenting e.g. 170 degrees as a real pitch value.
    if pitch > 90:
        pitch -= 180
    elif pitch < -90:
        pitch += 180

    return HeadPose(yaw=yaw, pitch=pitch, roll=roll, solve_success=True)
