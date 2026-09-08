# CV Validation Limitations

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA this was written against.

This document exists because Rule 3 of this pass's brief is explicit: *"Do not silently invent clinical certainty. These CV values are cosmetic proxy measurements unless validated otherwise."* Everything below is a plain statement of what the CV pipeline actually does and does not measure, and what corrupts each measurement — not a marketing description of it.

## Current state (Phases 7-11: NOT_IMPLEMENTED)

The CV pipeline (`backend/app/cv/`) is **unchanged from before this foundation pass** — none of Phase 7 (`CaptureAssessment`), Phase 8 (real head pose / `cv2.solvePnP`), Phase 9 (`MetricResult` for all 8 metrics), Phase 10 (per-metric confidence), or Phase 11 (abstention wired into the scorer) were implemented in this pass. This is stated plainly here and in `FOUNDATION_IMPLEMENTATION_REPORT.md` rather than left ambiguous.

What exists today:

- **Capture quality**: `CaptureQualityAssessor` (`app/cv/capture_quality.py`) returns a single blended float from sharpness, brightness, face-size, and MediaPipe's own detection confidence. There is no `PASS`/`BORDERLINE`/`FAIL` status, no yaw/pitch/roll, no structured `failure_reasons`.
- **Head pose**: does not exist. `grep -rn "solvePnP\|head_pose"` returns zero matches anywhere in `backend/app`. A photo taken at a steep angle is not detected as such at all.
- **The 8 metrics** (`evenness_score`, `redness_score`, `oiliness_score`, `texture_score`, `under_eye_darkness`, `puffiness_score`, `feature_definition_score`, `symmetry_score`, all in `app/cv/metric_extractors.py`) each return a **bare float**. There is no `MetricResult`, no per-metric confidence, no `uncertainty_reasons`, no abstention. A badly-lit or badly-posed photo produces a number that looks exactly as confident as a well-lit, well-posed one.

## What each metric actually measures, and what corrupts it (documented now, not yet enforced in code)

This is the analysis that Phase 10 would have turned into real confidence formulas. Recording it now so the next engineering pass doesn't have to re-derive it from scratch, and so nobody mistakes the current bare floats for validated measurements in the meantime.

| Metric | What it actually measures | Known corrupting factors | Clinical validity |
|---|---|---|---|
| `evenness_score` | Std. deviation of L\* (lightness) across forehead/cheek regions in LAB color space | Lighting uniformity, exposure, white balance, incomplete region extraction | Not validated against any dermatological pigmentation standard |
| `redness_score` | Mean a\* (red-green) channel offset in cheek/nose regions | Lighting color temperature, white balance, camera auto-processing, makeup, skin-tone calibration bias (LAB a\* baselines are not skin-tone-normalized here) | Not erythema; a color-channel proxy only |
| `oiliness_score` | Ratio of high-value/low-saturation ("specular highlight") pixels in the T-zone | Flash, specular studio lighting, moisturizer/sunscreen sheen, makeup, exposure | Not validated against sebum measurement of any kind |
| `texture_score` | Laplacian (high-frequency) variance in forehead/cheek regions | **Circular dependency with capture sharpness**: both texture and `CaptureQualityAssessor`'s own sharpness score use the same Laplacian-variance technique on overlapping regions. A blurry photo doesn't just fail the quality gate — if it passes, it also reads as artificially *smooth-skinned*. This is explicitly not solved in this pass. Also corrupted by resolution and camera-side sharpening. | Not a real texture/pore metric |
| `under_eye_darkness` | L\* gap between under-eye and cheek regions | Lighting direction/angle (side-lighting exaggerates shadow), exposure, head pose (no pose correction exists) | Not validated |
| `puffiness_score` | Heuristic profile-curvature/edge-count analysis on under-eye region brightness gradient | Lighting, pose, region-extraction quality, image resolution | Speculative heuristic, no external validation of any kind |
| `feature_definition_score` | Jaw angle sharpness + jawline-fit residual + chin z-projection from MediaPipe landmarks | **Strongly** sensitive to yaw/pitch/roll (a 15° head turn changes apparent jaw angle far more than any real anatomical difference would), camera distance, lens distortion | Not validated; pose sensitivity alone makes this metric unreliable without head-pose gating (Phase 8) |
| `symmetry_score` | Left/right landmark distance deviation from midline | **Most pose-sensitive metric in the pipeline.** Any yaw or roll directly fakes asymmetry — a perfectly symmetric face photographed at a 10° turn will score as asymmetric. Also sensitive to expression and landmark-detection quality | Not validated; currently the single most misleading metric to present as confident output without pose gating |

## Practical implication

Every plan this application currently generates is built from numbers that carry no confidence signal and no pose/lighting sanity check. A poorly-lit or off-angle photo does not get flagged, downgraded, or partially discounted — it produces a full plan with the same apparent authority as a well-captured one. This is the single largest correctness gap remaining after this pass; see `OPEN_ENGINEERING_ITEMS.md` for its priority classification.
