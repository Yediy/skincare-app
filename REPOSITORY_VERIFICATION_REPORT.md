# Repository Verification Report

**Date:** 2026-09-04
**Method:** Direct inspection of every tracked file in the repository (`git ls-files`), directory walks for untracked scaffolding, dependency inspection of the installed virtualenv, and attempted test execution. No claim below is taken on faith from any prior conversation.

## Repository inventory (ground truth)

Tracked files (`git ls-files`, 1 commit: `f9dc5cd`):

```
.gitignore
backend/.env.example
backend/app/__init__.py                  (empty)
backend/app/api/__init__.py              (empty)
backend/app/api/v2/__init__.py           (empty)
backend/app/cv/__init__.py               (empty)
backend/app/cv/capture_quality.py
backend/app/cv/face_landmarks.py
backend/app/cv/metric_extractors.py
backend/app/cv/pipeline.py
backend/app/db/__init__.py               (empty)
backend/app/domain/__init__.py           (empty)
backend/app/domain/priorities.py
backend/app/middleware/__init__.py       (empty)
backend/app/ml/__init__.py               (empty)
backend/app/ml/scorer.py
backend/app/observability/__init__.py    (empty)
backend/app/schemas/__init__.py          (empty)
backend/app/security/__init__.py         (empty)
backend/app/services/__init__.py         (empty)
backend/app/services/plan_service.py
backend/app/tasks/__init__.py            (empty)
backend/requirements.txt
docker-compose.yml
```

Untracked, on-disk but empty scaffolding:
- `mobile/src/{screens,components,context,navigation,api,services,hooks,state,config,observability}` — **0 files**, directories only.
- `backend/migrations/versions/` — **0 files**. No `alembic.ini`, no `env.py` — Alembic is not configured despite being in `requirements.txt`.

No `tests/` directory exists anywhere in the repo. No `Dockerfile` exists (only `docker-compose.yml`, which defines `db` and `redis` services only — no app container). No `main.py`/`asgi.py` app entrypoint exists, so **there is no `/api/v2/analyze` route, and no HTTP server at all.**

The virtualenv (`backend/.venv`) has only `mediapipe`, `opencv-python-headless`, and `opencv-contrib-python` installed. `fastapi`, `pytest`, `uvicorn`, and `alembic` — all listed in `requirements.txt` — are **not installed**, and nothing in the tracked code imports `jose`, `passlib`, `boto3`, `sentry_sdk`, `celery`, or `cryptography`, despite all being requirements-listed dependencies.

**Given this, the overwhelming majority of the 40 claims in Phase A, and most of Phases B–E, describe functionality that simply does not exist in this repository yet.** This is reported plainly below rather than inferred charitably from requirements.txt entries or directory names.

---

## Phase A — 40 claims

### Auth / Authorization

| # | Claim | Status | Evidence |
|---|---|---|---|
| 1 | `/me/...` self-service routes | **NOT_IMPLEMENTED** | `backend/app/api/` and `backend/app/api/v2/` contain only empty `__init__.py`. No route files, no `APIRouter` usage anywhere in the repo (`grep -rn "@router\|APIRouter"` → 0 hits). |
| 2 | Server derives ownership from auth only, never client `user_id` | **NOT_IMPLEMENTED** | No auth/session code exists to derive identity from (see #1). Nothing to verify. |
| 3 | Refresh-token families (`family_id`) | **NOT_IMPLEMENTED** | `backend/app/security/__init__.py` and `backend/app/db/__init__.py` are empty; no models, no migrations exist. `grep -r "family_id"` → 0 hits repo-wide. |
| 4 | Atomic refresh consumption (single UPDATE+WHERE) | **NOT_IMPLEMENTED** | No refresh-token code exists at all. |
| 5 | Refresh-token replay detection | **NOT_IMPLEMENTED** | Same as above. |
| 6 | Token-family revocation on replay | **NOT_IMPLEMENTED** | Same as above. |
| 7 | Session invalidation after account deletion/disable | **NOT_IMPLEMENTED** | No account deletion code, no session store, no mechanism of any kind. |
| 8 | Central `resetAppSession()` in mobile codebase | **NOT_IMPLEMENTED** | `mobile/` contains 0 files (directory scaffolding only). `grep -r "resetAppSession"` → 0 hits. |
| 9 | Logout/switch clears tokens, profile store, plan store, React Query cache, RevenueCat identity | **NOT_IMPLEMENTED** | No mobile source files exist at all to check any of the five sub-items individually. |

### Database / RLS

| # | Claim | Status | Evidence |
|---|---|---|---|
| 10 | Runtime DB role is non-superuser | **NOT_IMPLEMENTED / UNVERIFIABLE AS DESCRIBED** | `docker-compose.yml` defines a single `postgres` superuser role (`POSTGRES_USER=postgres`) with no secondary application role created anywhere (no init SQL, no migration creates a role). The runtime role **is** superuser. |
| 11 | Runtime role doesn't own protected tables | **NOT_IMPLEMENTED** | No tables exist — no migrations have been written (`backend/migrations/versions/` is empty), so there are no protected tables to own or not own. |
| 12 | No BYPASSRLS on runtime role | **NOT_IMPLEMENTED** | The only configured role is `postgres` itself, which is always superuser and implicitly bypasses RLS. Not verifiable against a real "restricted app role" because none exists. I did not have a live Postgres instance running to query `pg_roles` against, and there is nothing meaningful to query regardless — noting this explicitly per the ground rules rather than guessing. |
| 13 | `SET LOCAL app.current_user_id` used in real query paths | **NOT_IMPLEMENTED** | `grep -r "SET LOCAL\|current_user_id"` → 0 hits repo-wide. No query/DB-access code of any kind exists (`backend/app/db/__init__.py` is empty). |
| 14 | Connection-pool reuse cannot leak user context (tested) | **NOT_IMPLEMENTED** | No connection pool exists (no DB driver usage at all — `asyncpg`/`psycopg2` are in requirements.txt but unimported anywhere). |
| 15 | Cross-user RLS tests exist and pass against real Postgres | **NOT_IMPLEMENTED** | No `tests/` directory exists anywhere in the repo. No RLS policies exist to test. |

### Encryption / Privacy

| # | Claim | Status | Evidence |
|---|---|---|---|
| 16 | Legacy sensitive fields encrypted (migration 018) | **NOT_IMPLEMENTED** | `backend/migrations/versions/` contains 0 files. There is no migration 018 or any migration. |
| 17 | Encryption stores key/version metadata | **NOT_IMPLEMENTED** | No encryption code exists (`cryptography` package is unused — 0 imports repo-wide despite being in requirements.txt). |
| 18 | No legacy plaintext remains post-migration | **NOT_IMPLEMENTED / UNVERIFIABLE** | No migration exists to have run, and there is no live production data to inspect. Stating explicitly per the ground rules rather than guessing. |
| 19 | Raw face photos not written to AsyncStorage | **NOT_IMPLEMENTED (nothing to check)** | `mobile/` has 0 source files. `grep -r "AsyncStorage"` → 0 hits. There is no mobile capture flow at all yet, so the claim describes work that hasn't started. |
| 20 | Raw photos not logged | **PARTIALLY_IMPLEMENTED** | `backend/app/cv/pipeline.py:45` logs `f"Analysis complete: quality={quality_score:.2f}, metrics={metrics}"` — this logs derived metrics only, not raw bytes, which is correct practice. However `backend/app/cv/capture_quality.py:20` logs a component-score breakdown dict, also derived, not raw. No raw-byte logging found anywhere. This is incidentally correct but there is no test or explicit policy enforcing it — it's just that no code path currently logs images. |
| 21 | Sensitive values excluded from Sentry via `before_send` | **NOT_IMPLEMENTED** | `sentry_sdk` is listed in requirements.txt but has 0 imports anywhere in the repo. Sentry is not initialized at all. |
| 22 | Account deletion integration test against populated user | **NOT_IMPLEMENTED** | No account deletion code exists; no `tests/` directory exists. |

### Billing / Quotas

| # | Claim | Status | Evidence |
|---|---|---|---|
| 23 | RevenueCat `CANCELLATION` doesn't immediately revoke | **NOT_IMPLEMENTED** | `grep -r "RevenueCat\|CANCELLATION"` → 0 hits repo-wide. No webhook handling code exists at all. |
| 24 | `EXPIRATION` ends access | **NOT_IMPLEMENTED** | Same — no billing/webhook code exists. |
| 25 | Out-of-order webhook protection | **NOT_IMPLEMENTED** | No webhook handler exists (`backend/app/api/` is empty). |
| 26 | Duplicate webhook idempotency | **NOT_IMPLEMENTED** | Same. |
| 27 | Atomic usage/quota reservation (real SQL) | **NOT_IMPLEMENTED** | No quota code, no SQL, no DB access layer exists anywhere. |
| 28 | Concurrent quota tests | **NOT_IMPLEMENTED** | No `tests/` directory exists. |
| 29 | Atomic Redis/Lua rate limiting wired into middleware | **NOT_IMPLEMENTED** | `backend/app/middleware/__init__.py` is empty. `redis` package is a requirement but has 0 imports anywhere. No Lua scripts exist in the repo. |
| 30 | Concurrent rate-limit tests | **NOT_IMPLEMENTED** | No `tests/` directory exists. |

### Recommendation Safety

| # | Claim | Status | Evidence |
|---|---|---|---|
| 31 | Allergies enforced at ingredient level (SQL in `offer_repository.py`) | **NOT_IMPLEMENTED** | No file named `offer_repository.py` exists anywhere in the repo (`find . -iname "offer_repository*"` → 0 results). There is no offer/product catalog, no SQL query layer at all. `PlanService._extract_user_constraints` (`backend/app/services/plan_service.py:93-102`) reads an `allergies` key from the input `user_profile` dict into a constraints dict, but **that value is never subsequently read or enforced anywhere** in `plan_service.py` — it is dead data. `grep -n "allergies" backend/app/services/plan_service.py` shows exactly one write and zero reads. |
| 32 | `avoid_ingredients` enforced | **NOT_IMPLEMENTED** | Identical situation to #31 — `avoid_ingredients` is captured into the constraints dict at `plan_service.py:101` and never read again anywhere in the file or repo. |
| 33 | Sensitive-skin rules materially alter interventions | **PARTIALLY_IMPLEMENTED** | `has_sensitive_skin` is captured (`plan_service.py:97`) and used exactly once, at `plan_service.py:362-363`, to append a disclaimer string ("Recommendations have been adjusted for sensitive skin..."). It does **not** alter routine steps, product categories, or intensity anywhere — no code branches on `has_sensitive_skin` other than that one disclaimer append. This is precisely the "just a disclaimer sentence" pattern the claim explicitly says should NOT be the case, so it does not meet the claimed bar. |
| 34 | Pregnancy/nursing restrictions structurally supported | **PARTIALLY_IMPLEMENTED** | This one is real: `plan_service.py:111-113` — `_apply_compatibility_constraints` skips any priority whose `compatibility_restrictions` includes `CompatibilityRule.PREGNANCY_RESTRICTED` when `is_pregnant` or `is_nursing` is set. This structurally removes the `TEXTURE_SMOOTHING` priority (the only one tagged `PREGNANCY_RESTRICTED` in `domain/priorities.py:94`) rather than just disclaiming it. This is a real, wired mechanism — but it is untested (no `tests/` dir) and only covers one priority definition; no equivalent restriction exists for the ingredient/allergy paths (see #31/#32). |
| 35 | Automatic supplement recommendations removed from photo-derived planning | **NOT_IMPLEMENTED — in fact the opposite is true** | `plan_service.py:34` unconditionally calls `self._build_supplement_recommendations(priority_ids)`, and `_build_supplement_recommendations` (`plan_service.py:297-322`) actively recommends specific supplements (e.g., "Vitamin C (1000mg)", "Vitamin A", "Creatine") keyed directly off photo-derived priority IDs, and includes the result unconditionally in the returned plan at `plan_service.py:53`. There is no removal, no flag, no gating — automatic supplement recommendations from photo-derived priorities are fully present and active in the only plan-generation code path that exists. |
| 36 | Affiliate commission cannot override safety/suitability in ranking | **NOT_IMPLEMENTED** | No affiliate/commission concept exists anywhere in the repo (`grep -ri "commission\|affiliate"` → 0 hits). `_rank_priorities_by_severity` (`plan_service.py:75-91`) ranks purely by severity/pillar-score/display-order; there is no product-level ranking or offer-matching code at all, so there is nothing that could be overridden by commission in the first place. The claim describes a system that hasn't been started. |

### Deployment

| # | Claim | Status | Evidence |
|---|---|---|---|
| 37 | `.dockerignore` exists and excludes secrets | **NOT_IMPLEMENTED** | `find . -iname ".dockerignore"` → 0 results. |
| 38 | Genuinely multi-stage Dockerfile | **NOT_IMPLEMENTED** | No `Dockerfile` exists anywhere in the repo. `docker-compose.yml` only defines `db` (postgres:15-alpine) and `redis` (redis:7-alpine) — there is no application service/image defined at all. |
| 39 | Runtime container runs non-root | **NOT_IMPLEMENTED** | No Dockerfile exists, so there is no runtime container to check. |
| 40 | Startup rejects unsafe placeholder/default config | **NOT_IMPLEMENTED** | There is no app startup code at all (no `main.py`). `backend/.env.example` ships `JWT_SECRET=` and `API_KEY=` as blank placeholders and `ALLOWED_ORIGINS=["*"]`, and nothing reads or validates these anywhere in the tracked code, so nothing could reject them even if the app existed. |

---

## Phase B — Capture/measurement work

| # | Claim | Status | Evidence |
|---|---|---|---|
| 1 | `CaptureAssessment` class exists | **NOT_IMPLEMENTED (different design exists)** | No class named `CaptureAssessment` exists anywhere (`grep -r "CaptureAssessment"` → 0 hits). What exists instead is `CaptureQualityAssessor` in `backend/app/cv/capture_quality.py:8`, whose `.assess()` method returns a single bare `float` (a weighted blend of sharpness/brightness/face-size/detection-confidence), not a structured result object. |
| 2 | Called by real `/analyze` production route | **NOT_IMPLEMENTED** | There is no `/analyze` route or any HTTP route at all (`backend/app/api/` is empty). `FacialAnalysisPipeline.analyze()` (`backend/app/cv/pipeline.py:32`) does call `CaptureQualityAssessor.assess()` at `pipeline.py:39`, so the quality assessor **is** wired into the pipeline class itself — but the pipeline class itself is never imported or invoked from anywhere (`grep -rn "FacialAnalysisPipeline" --include="*.py"` finds only its own definition; no importer exists). It is dead code from the perspective of a running service. |
| 3 | Claimed fields (quality_status, yaw, pitch, roll, blur, exposure, lighting_balance, occlusion, resolution, failure_reasons) | **NOT_IMPLEMENTED** | None of these fields/names exist anywhere in the repo. `CaptureQualityAssessor` only tracks `sharpness`, `brightness`, `face_size`, `detection_confidence` (`capture_quality.py:10-14`) — a different and much smaller set than claimed. |
| 4 | Head pose via `cv2.solvePnP` | **NOT_IMPLEMENTED** | `grep -rn "solvePnP"` → 0 hits repo-wide. No head-pose/yaw/pitch/roll estimation exists in any form. |
| 5 | Excessive yaw/pitch/roll blocks/downgrades capture | **NOT_IMPLEMENTED** | No yaw/pitch/roll values are computed at all (see #4), so nothing can threshold on them. |
| 6 | FAIL prevents analysis (route returns before scorer) | **PARTIALLY_IMPLEMENTED, at the pipeline level only, not a route** | `FacialAnalysisPipeline.analyze()` does raise `LowQualityCaptureError` (`pipeline.py:40-41`) before calling `self.metric_extractor.compute_all_metrics(...)` at `pipeline.py:43`, so *within this one class*, a low-quality capture does prevent metric extraction. But since there is no route calling this pipeline (#2), this guarantee is currently unreachable from any real request. |
| 7 | BORDERLINE status distinct from PASS | **NOT_IMPLEMENTED** | There is no status enum at all — only a raw float compared against one single threshold (`MIN_ACCEPTABLE_QUALITY = 0.35`, `pipeline.py:21`). There are exactly two outcomes (raise or don't), no BORDERLINE concept exists. |
| 8 | Capture assessment persisted as derived metadata only, no raw bytes to DB | **NOT_IMPLEMENTED / N/A** | There is no persistence layer at all (`backend/app/db/__init__.py` is empty, no DB writes exist anywhere in tracked code), so nothing is persisted — correct by total absence, not by a data-handling guarantee that was actually built and verified. |

---

## Phase C — Confidence/abstention per metric

`backend/app/cv/metric_extractors.py`'s `SkinMetricExtractor.compute_all_metrics()` (line 15) computes exactly **8 metrics**, confirmed by direct count: `evenness_score`, `redness_score`, `oiliness_score`, `texture_score`, `under_eye_darkness`, `puffiness_score`, `feature_definition_score`, `symmetry_score` (lines 17-24).

**For all 8 metrics, all 7 sub-questions resolve identically, because no confidence/abstention system exists at all:**

| Sub-question | Status (applies to all 8 metrics identically) |
|---|---|
| 1. Returns `MetricResult` structure? | **NOT_IMPLEMENTED.** Every `_compute_*` method returns a bare Python `float` (e.g. `metric_extractors.py:47,69,89,111,148,193,203,273`). No class or type named `MetricResult` exists anywhere in the repo. |
| 2. What determines confidence, traced | **NOT_IMPLEMENTED.** No per-metric confidence value is computed or returned. The *only* related concept in the codebase is a single scalar `capture_quality` float computed once per whole image by `CaptureQualityAssessor` (Phase B) and passed into `FacialScorer.compute_scores(..., capture_quality=...)` (`scorer.py:41`) as a blanket `confidence` multiplier applied uniformly to every metric's severity (`scorer.py:25` in `PriorityTrigger.should_trigger`). It is not metric-specific and does not originate from anything in `metric_extractors.py`. |
| 3. Abstention threshold, reachable? | **NOT_IMPLEMENTED.** There is no abstention concept — every metric always returns a numeric value, including hardcoded fallback constants (e.g. `0.5`, `0.3`, `0.4`) used when a region can't be extracted (e.g. `metric_extractors.py:43,65,74,141,190`). A missing/degraded region silently substitutes a fixed guess rather than abstaining or flagging low confidence. |
| 4. Specific `uncertainty_reasons`, do they match real corruption causes? | **NOT_IMPLEMENTED.** `grep -r "uncertainty_reason"` → 0 hits repo-wide. No reason codes of any kind are attached to metric output. |
| 5. Deterministic test exists? | **NOT_IMPLEMENTED.** No `tests/` directory exists anywhere in the repo. |
| 6. Poor-capture-condition test exists? | **NOT_IMPLEMENTED.** Same — no tests exist at all. |
| 7. Boundary test at abstain/borderline threshold? | **NOT_IMPLEMENTED.** No such threshold exists to have a boundary test for, and no tests exist regardless. |

**Note on the fallback constants:** several of these (e.g. `_compute_redness` returning `0.3` when no cheek/nose pixels are extracted, `metric_extractors.py:65`) are silent low-information defaults presented with the same shape as a real measurement — this is a correctness risk beyond just "untested," since a badly-cropped face could produce a confident-looking but meaningless score with no signal anywhere downstream that it happened.

---

## Phase D — Structured Safety Decisions

| # | Claim | Status | Evidence |
|---|---|---|---|
| 1 | `SafetyDecision` exists with claimed fields | **NOT_IMPLEMENTED** | `grep -r "SafetyDecision"` → 0 hits anywhere in the repo. |
| 2 | `SafetyEngine` called by `PlanService`, or old inline filtering never removed | **NOT_IMPLEMENTED (the "never removed" branch is what's true)** | `grep -r "SafetyEngine"` → 0 hits — the class does not exist at all. `PlanService` (`plan_service.py`) contains only its own original inline filtering method, `_apply_compatibility_constraints` (lines 104-134), which directly branches on `CompatibilityRule` enum membership. There was never a `SafetyEngine` to remove this in favor of. |
| 3 | `evaluate_offer()` vs `evaluate_priorities()` called | **NOT_IMPLEMENTED** | Neither method exists anywhere; there is no offer-evaluation code and no safety-engine-style priorities evaluation separate from the plain inline filtering already described. |
| 4 | Rejected candidates excluded from final plan vs. just recorded | **PARTIALLY_IMPLEMENTED, at the priority level only — no offers/candidates exist to test the distinction on** | `_apply_compatibility_constraints` does `continue` (true exclusion, not just flagging) for priorities that fail pregnancy/nursing or beginner-intensity checks (`plan_service.py:112-113,126-127`), and excluded priorities do not appear in `filtered_priorities`, so at the *priority* level, exclusion is real, not cosmetic. However there is no product/offer candidate list anywhere in the repo, so the specific claim about "candidates" (implying a product/offer catalog) cannot be verified because that catalog doesn't exist. |
| 5 | Reason codes are consistent, machine-readable | **NOT_IMPLEMENTED** | No reason codes are recorded for filtering decisions at all — `_apply_compatibility_constraints` just silently omits an ID from its return list; nothing downstream records *why* a given priority was dropped. |

---

## Phase E — End-to-end integration test

**I did not fabricate this test.** Per the ground rules ("if something can't be determined ... say so explicitly rather than guessing"), attempting to write the requested end-to-end test (signup → consent → quota → capture → metrics → priorities → safety → routine → offers → plan persisted) would require inventing from scratch nearly every piece of infrastructure this report just found missing: there is no auth/signup, no consent model, no quota system, no HTTP route to call for capture, no persistence layer, no offer catalog, and no `SafetyEngine`. Writing a "test" against pipeline classes called directly in Python would not be testing the real system described in the scenarios — it would be testing whether I could hand-wire pieces together in a script, which is not the same claim and would misrepresent the repository's actual state. I ran this instead:

```
cd backend && .venv/bin/python -m pytest tests/ -v
```
Result: `No module named pytest` — `pytest` is listed in `requirements.txt` but is not installed in the tracked virtualenv, and no `tests/` directory exists to point it at regardless.

| Scenario | Status |
|---|---|
| 1. Good capture → analysis succeeds | **UNTESTABLE AS AN INTEGRATION TEST** — `FacialAnalysisPipeline.analyze()` (unwired, Phase B #2) could be called directly in isolation, but this is not the "authenticated → persisted" flow the scenario describes, since none of the surrounding infrastructure exists. |
| 2. Excessive yaw → capture rejected/unusable | **NOT_IMPLEMENTED** — no yaw computation exists at all (Phase B #4). |
| 3. Poor lighting → affected metrics abstain | **NOT_IMPLEMENTED** — no abstention concept exists (Phase C). |
| 4. Allergy conflict → candidate removed, reason recorded | **NOT_IMPLEMENTED** — allergy data is captured but never read (Phase A #31); no offer catalog exists to remove a candidate from. |
| 5. Sensitive skin → intensity reduced, concern still shown | **NOT_IMPLEMENTED** — sensitive skin only appends a disclaimer string (Phase A #33); intensity is never altered. |
| 6. Beginner → concern shown, intensity changed | **PARTIALLY_IMPLEMENTED** — `_apply_compatibility_constraints` does drop `advanced`-intensity priorities entirely for beginners (`plan_service.py:125-127`) rather than showing the concern with reduced intensity — so the *concern itself disappears* rather than the *intensity changing*, which is arguably the opposite of the intended behavior in the scenario. |
| 7. Higher-commission product cannot bypass safer lower-commission one in ranking | **NOT_IMPLEMENTED** — no commission concept, no product ranking, no offer matching exists anywhere (Phase A #36). |

**I am explicitly not claiming any of these seven passed.** No new test files or integration scaffolding were added to the repository as part of this verification, since doing so would have meant building speculative future feature code rather than verifying what exists — which the task's own ground rules and final instruction ("do not start any new feature work") both preclude.

---

## Summary of what is real and correctly wired

Out of everything asked about above, the components that are genuinely implemented, non-trivial, and internally self-consistent are:

- `backend/app/domain/priorities.py` — a real, reasonably well-designed static priority/rule table (8 `PriorityDef` entries across 4 pillars), used consistently by both `scorer.py` and `plan_service.py`.
- `backend/app/ml/scorer.py` — `FacialScorer`/`PriorityTrigger` genuinely consume `metric_extractors.py` output and `domain/priorities.py` thresholds to produce triggered-priority lists with severity scores. This is real, wired logic.
- `backend/app/services/plan_service.py` — `PlanService.generate_plan()` genuinely consumes scorer output to build AM/PM routines, eye care, facial toning, lifestyle tips, and (unintentionally, per claim #35) supplement recommendations. The pregnancy/nursing exclusion (Phase A #34) is real.
- `backend/app/cv/*.py` — a real, non-stubbed CV metric pipeline using MediaPipe FaceMesh landmarks and OpenCV region analysis to compute 8 skin/face metrics from an image, plus a separate quality-gating pipeline (`FacialAnalysisPipeline`). This is legitimate, functioning image-processing code — it is simply **not connected to anything** (no HTTP layer exists to call it, and it has no tests).

Everything else claimed in Phases A, B (fields 1/3/4/5/7), C, D, and the E scenarios does not exist in this repository as of commit `f9dc5cd`.
