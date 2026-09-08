# CV Validation Limitations

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA.

This document exists because Rule 3 of the foundation-pass brief is explicit: *"Do not silently invent clinical certainty. These CV values are cosmetic proxy measurements unless validated otherwise."* Everything below is a plain statement of what the CV pipeline actually does and does not measure, and what corrupts each measurement — not a marketing description of it. **Phases 7-11 are now implemented** (see below) — this supersedes the earlier version of this document, written when none of them existed. Implementing them made every claim here *enforced in code*; it did not make any of the underlying formulas clinically validated. That distinction is the entire point of this document and hasn't changed.

## Current state

| Phase | Status |
|---|---|
| 7. `CaptureAssessment` (structured PASS/BORDERLINE/FAIL) | **Implemented** — `app/cv/capture_assessment.py` |
| 8. Real head pose (`cv2.solvePnP`) | **Implemented** — `app/cv/head_pose.py` |
| 9. `MetricResult` for all 8 metrics | **Implemented** — `app/cv/metric_result.py`, wired via `SkinMetricExtractor.compute_all_metric_results()` |
| 10. Per-metric confidence | **Implemented** — `app/cv/metric_confidence.py`, one formula per metric, derived from the actual corrupting factors listed below |
| 11. Abstention wired into scorer | **Implemented** — `FacialScorer._analyze_pillar()` skips triggering for any `ABSTAINED` metric outright |

Head pose (yaw/pitch/roll) is a **monocular, uncalibrated-camera estimate**: `cv2.solvePnP` against a generic 3D face model (average adult face proportions, not the actual subject's measurements) and a pinhole camera model with focal length approximated from image width (there is no real camera calibration for an arbitrary phone/webcam photo). This is adequate for gating excessive pose and for confidence weighting — it is not precision metrology, and `HeadPose`/`CaptureAssessment` never claim otherwise.

`calibration_version` on every `MetricResult` is literally the string `"uncalibrated-1.0"` — this is deliberate, not a placeholder that got forgotten. There is no validated calibration dataset behind any confidence formula below. The thresholds (`ABSTAIN_THRESHOLD = 0.35`, `BORDERLINE_THRESHOLD = 0.60`, pose gates at 15°/30°) are reasonable-sounding, internally-consistent starting points, proven *reachable in practice* by real tests (see `TEST_REPORT.md`) — not values derived from any clinical or photographic validation study.

## What each metric actually measures, what corrupts it, and how confidence now models that (`app/cv/metric_confidence.py`)

| Metric | What it actually measures | Known corrupting factors | Confidence formula (weights sum to 1.0) |
|---|---|---|---|
| `evenness_score` | Std. deviation of L\* (lightness) across forehead/cheek regions in LAB color space | Lighting uniformity, exposure, incomplete region extraction | `0.40·lighting_balance + 0.35·exposure_score + 0.25·face_size_score` |
| `redness_score` | Mean a\* (red-green) channel offset in cheek/nose regions | Lighting color temperature/white balance (no real color-temp sensor exists — `lighting_balance` is the closest available proxy), exposure, skin-tone calibration bias | `0.50·lighting_balance + 0.50·exposure_score` |
| `oiliness_score` | Ratio of high-value/low-saturation ("specular highlight") pixels in the T-zone | Flash, specular lighting, exposure (the highlight-ratio technique is directly exposure-sensitive) | `0.60·exposure_score + 0.40·lighting_balance` |
| `texture_score` | Laplacian (high-frequency) variance in forehead/cheek regions | **Still an explicit, documented circular dependency**: this metric and the capture pipeline's own `blur_score` use the same Laplacian-variance technique on overlapping regions. A blurry photo doesn't just fail the quality gate — if it's borderline rather than failed, it *also* reads as artificially smooth-skinned. Not solved by this pass; weighted honestly instead of ignored. | `0.70·blur_score + 0.30·resolution_score` |
| `under_eye_darkness` | L\* gap between under-eye and cheek regions | Lighting direction, exposure, head pose (pitch especially) | `0.35·lighting_balance + 0.35·exposure_score + 0.30·(1 − 0.6·pose_severity)` |
| `puffiness_score` | Heuristic profile-curvature/edge-count analysis on under-eye brightness gradient | Lighting, pose, resolution | `0.30·lighting_balance + 0.40·(1 − 0.6·pose_severity) + 0.30·resolution_score` |
| `feature_definition_score` | Jaw angle sharpness + jawline-fit residual + chin z-projection | **Strongly** pose-sensitive — a real head turn changes apparent jaw angle far more than any anatomical difference would | `0.75·(1 − pose_severity) + 0.25·resolution_score` |
| `symmetry_score` | Left/right landmark distance deviation from midline | **The single most pose-sensitive metric in the pipeline** — any yaw or roll directly fakes asymmetry | `0.85·(1 − pose_severity) + 0.15·blur_score` |

`pose_severity` = `max(|yaw|, |pitch|, |roll|) / 45°`, clipped to `[0, 1]`; a head pose that couldn't be solved at all (`solve_success = False`) is treated as **maximum** severity (1.0), never a suspiciously confident 0 — proven directly by a real test (`test_undetermined_head_pose_is_treated_as_maximum_severity_not_zero`).

None of these formulas were derived from a validation dataset. They encode a defensible, documented hypothesis about which capture-quality signals plausibly corrupt which metric — proven internally consistent and reachable (a metric genuinely does abstain when its specific corrupting factors are bad enough, verified by real tests including boundary cases), not proven clinically accurate.

## What's still not solved

- **The texture/blur circular dependency** (see table above) is documented, not fixed — fixing it for real would need a texture-extraction technique independent of the same Laplacian-variance signal the capture gate uses, which is a real, separate piece of future work.
- **No real color-temperature or white-balance measurement exists.** `lighting_balance` (a left/right face-half brightness-gap proxy) stands in for it across `evenness`, `redness`, and `oiliness` confidence — a reasonable proxy for *directional* lighting problems, not a substitute for measuring actual color temperature.
- **`occlusion_score` is a reused detection-confidence proxy**, not a real occlusion classifier — MediaPipe FaceMesh doesn't expose per-landmark visibility for this model.
- **Thresholds are hand-picked**, not fit to any dataset. `MAX_ANGLE_PASS`/`MAX_ANGLE_BORDERLINE` (15°/30°), `ABSTAIN_THRESHOLD`/`BORDERLINE_THRESHOLD` (0.35/0.60), and the confidence-formula weights themselves are all reasonable starting points with real test coverage proving their *mechanics* work — not values a calibration study has validated as clinically or photographically correct.

## Practical implication (updated from the prior version of this document)

Before this pass, every plan was built from numbers with no confidence signal at all — a badly-lit or badly-posed photo produced a full plan with the same apparent authority as a well-captured one, silently. That is no longer true: a genuinely poor capture now either fails outright (blocking analysis entirely) or degrades specific metrics into `BORDERLINE`/`ABSTAINED`, and an abstained metric is now provably incapable of triggering a personalized concern (`tests/cv/test_scorer_abstention.py`, `tests/integration/test_end_to_end_analysis.py::test_poor_lighting_causes_a_color_metric_to_abstain`). What remains true, and must not be lost in the relief of that: none of this is clinically validated. The system now correctly *knows when it doesn't know* — it still doesn't know whether what it does report, when confident, is dermatologically accurate.
