"""Phase 8: real head-pose estimation, tested with synthetic/controlled
landmark configurations (per the brief's own guidance) -- no real photo
or MediaPipe detection pass required, so these run fast and
deterministically."""
import numpy as np

from app.cv.head_pose import estimate_head_pose, NOSE_TIP_IDX, CHIN_IDX, \
    LEFT_EYE_CORNER_IDX, RIGHT_EYE_CORNER_IDX, LEFT_MOUTH_CORNER_IDX, RIGHT_MOUTH_CORNER_IDX

IMG_W, IMG_H = 800, 800


def _make_landmarks(nose, chin, left_eye, right_eye, left_mouth, right_mouth):
    landmarks = np.zeros((478, 3))
    landmarks[NOSE_TIP_IDX, :2] = nose
    landmarks[CHIN_IDX, :2] = chin
    landmarks[LEFT_EYE_CORNER_IDX, :2] = left_eye
    landmarks[RIGHT_EYE_CORNER_IDX, :2] = right_eye
    landmarks[LEFT_MOUTH_CORNER_IDX, :2] = left_mouth
    landmarks[RIGHT_MOUTH_CORNER_IDX, :2] = right_mouth
    return landmarks


def _frontal_landmarks():
    return _make_landmarks(
        nose=(0.50, 0.55),
        chin=(0.50, 0.85),
        left_eye=(0.35, 0.45),
        right_eye=(0.65, 0.45),
        left_mouth=(0.40, 0.70),
        right_mouth=(0.60, 0.70),
    )


def _yawed_landmarks():
    """Simulates a head turned to one side: the far-side features
    compress toward the face center (foreshortened by perspective),
    the near-side features stay wide, and the nose shifts off the eye
    midline -- the standard visual signature of yaw."""
    return _make_landmarks(
        nose=(0.40, 0.55),       # shifted off the eye midline (0.50)
        chin=(0.42, 0.85),
        left_eye=(0.30, 0.45),   # near side: stays wide
        right_eye=(0.52, 0.45),  # far side: compressed toward center
        left_mouth=(0.34, 0.70),
        right_mouth=(0.50, 0.70),
    )


def test_frontal_face_yields_near_zero_pose():
    pose = estimate_head_pose(_frontal_landmarks(), IMG_W, IMG_H)
    assert pose.solve_success is True
    assert abs(pose.yaw) < 15.0
    assert abs(pose.pitch) < 15.0
    assert abs(pose.roll) < 15.0


def test_yawed_face_yields_larger_yaw_magnitude_than_frontal():
    frontal = estimate_head_pose(_frontal_landmarks(), IMG_W, IMG_H)
    yawed = estimate_head_pose(_yawed_landmarks(), IMG_W, IMG_H)
    assert abs(yawed.yaw) > abs(frontal.yaw)
    assert abs(yawed.yaw) > 15.0


def test_solve_failure_is_reported_not_silently_defaulted():
    """Degenerate (all-identical) points can't be solved for pose --
    solvePnP should report failure rather than us silently returning
    a fake zero-pose that looks identical to a real frontal capture."""
    degenerate = _make_landmarks(
        nose=(0.5, 0.5), chin=(0.5, 0.5), left_eye=(0.5, 0.5),
        right_eye=(0.5, 0.5), left_mouth=(0.5, 0.5), right_mouth=(0.5, 0.5),
    )
    pose = estimate_head_pose(degenerate, IMG_W, IMG_H)
    # Either it reports failure, or (if solvePnP tolerates the
    # degeneracy) it must not silently claim a confident zero pose --
    # accept either an explicit failure or a huge/undefined-looking
    # angle, but never a suspiciously clean 0.0/0.0/0.0 success.
    if pose.solve_success:
        assert not (pose.yaw == 0.0 and pose.pitch == 0.0 and pose.roll == 0.0)
