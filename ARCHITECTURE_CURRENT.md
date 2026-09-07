# Architecture — As It Actually Exists

**This describes only what is present in the repository at commit `f9dc5cd`, verified by direct file inspection.** It intentionally does not describe any planned auth, database, billing, or mobile architecture — none of that exists yet.

## What this repository actually is

A standalone Python **domain-logic prototype** for turning a face photo into a skincare plan. It is a set of plain, importable Python classes with no web framework wired up, no database, no tests, and no mobile client. It cannot currently be run as a service — there is no server entrypoint.

## Directory layout (tracked code only)

```
backend/
  app/
    cv/               ← real code: image → face landmarks → 8 skin metrics + a quality score
      face_landmarks.py     FaceLandmarkExtractor (MediaPipe FaceMesh), region cropping
      metric_extractors.py  SkinMetricExtractor: 8 metrics from landmark regions
      capture_quality.py    CaptureQualityAssessor: single float quality score
      pipeline.py           FacialAnalysisPipeline: orchestrates the above (UNUSED — no caller)
    domain/
      priorities.py    Static rule table: 8 PriorityDef entries across 4 pillars
    ml/
      scorer.py         FacialScorer/PriorityTrigger: metrics → triggered priorities + severity
    services/
      plan_service.py   PlanService: priorities → AM/PM routine, eye care, lifestyle, disclaimers
    api/, api/v2/, db/, security/, middleware/, tasks/, schemas/, observability/
                        ← all empty __init__.py stub packages, no content
  requirements.txt      lists fastapi/pytest/alembic/jose/passlib/boto3/sentry/celery/cryptography
                        — NONE of these are installed or imported anywhere in tracked code
docker-compose.yml       postgres:15-alpine + redis:7-alpine only — no app container, no Dockerfile
mobile/                  empty directory scaffolding, 0 files
```

## The one real data flow that exists (not wired to anything)

```
image bytes
  → FacialAnalysisPipeline.analyze()          [backend/app/cv/pipeline.py]
      → FaceLandmarkExtractor.extract()       [cv/face_landmarks.py]  → MediaPipe FaceMesh landmarks
      → CaptureQualityAssessor.assess()       [cv/capture_quality.py] → single float quality score
          (raises LowQualityCaptureError if quality < 0.35 — analysis stops here)
      → SkinMetricExtractor.compute_all_metrics() [cv/metric_extractors.py]
          → 8 floats: evenness, redness, oiliness, texture, under_eye_darkness,
                       puffiness, feature_definition, symmetry
  → { metrics: {...8 floats...}, capture_quality: float }
```

**Nothing calls `FacialAnalysisPipeline`.** It is fully-formed, real image-processing code, but it is dead code from a running-system perspective — there is no route, no CLI, no test, nothing in the tracked repo that imports `app.cv.pipeline`.

## The one other real data flow that exists (also not wired to an entrypoint, but self-consistent)

```
{8 metric floats} + capture_quality + optional historical_priorities
  → FacialScorer.compute_scores()             [backend/app/ml/scorer.py]
      → for each of the 8 PriorityDef entries in domain/priorities.py:
          PriorityTrigger.should_trigger() compares metric value to severity_threshold,
          scales by capture_quality and historical persistence
      → { scores: {4 pillar scores}, insights: {4 pillars: triggered priority IDs + severities} }

  → PlanService.generate_plan(scores, insights, user_profile, capture_quality)
      [backend/app/services/plan_service.py]
      → ranks triggered priorities by severity + pillar weight + display order
      → _apply_compatibility_constraints(): drops priorities incompatible with
          pregnancy/nursing (real exclusion) or beginner intensity (real exclusion);
          allergy/avoid_ingredients data is captured into a constraints dict but never read
      → builds AM routine, PM routine, eye care block, facial toning block, lifestyle
          recommendations, and supplement recommendations (unconditional, from photo-derived
          priorities — no gating flag exists to suppress this)
      → returns one plan dict with disclaimers and metadata
```

Both `FacialScorer` and `PlanService` are plain, synchronous, framework-free Python classes. They accept plain dicts in and return plain dicts out — nothing here depends on FastAPI, a database, or any web concept. They could be called from a script or a notebook today, but there is no code anywhere in the repo that does so.

## What does not exist, at all

- **No web/API layer.** `app/api/` and `app/api/v2/` are empty packages. No route, no request/response schema (`app/schemas/` is empty), no ASGI app object, no `main.py`.
- **No database.** `app/db/` is empty. No SQLAlchemy/asyncpg models, no queries, no connection pooling code. `docker-compose.yml` starts a bare Postgres container with no app-level role, schema, or RLS policy — just the vendor default `postgres` superuser.
- **No migrations.** `backend/migrations/versions/` exists as an empty directory; Alembic is not configured (no `alembic.ini`, no `env.py`).
- **No auth.** `app/security/` is empty. No JWT issuance/verification, no refresh tokens, no session model, despite `python-jose` and `passlib` being requirements-listed.
- **No middleware.** `app/middleware/` is empty — no rate limiting, no request logging, no auth guard.
- **No background tasks.** `app/tasks/` is empty; `celery` is requirements-listed but never imported.
- **No observability.** `app/observability/` is empty; `sentry-sdk` is requirements-listed but never imported or initialized.
- **No billing/webhooks.** No RevenueCat, Stripe, or any payment-provider integration exists anywhere.
- **No offer/product catalog.** No `offer_repository.py` or equivalent exists; there is nothing that maps a priority to an actual purchasable product, and no commission/affiliate concept exists.
- **No safety engine.** No `SafetyEngine`/`SafetyDecision` abstraction exists — the only filtering logic is the inline `_apply_compatibility_constraints` method inside `PlanService`.
- **No tests.** No `tests/` directory exists anywhere in the repository, for any layer.
- **No mobile app.** `mobile/` is an empty directory tree (10 empty subdirectories, 0 files).
- **No Docker image for the app itself.** `docker-compose.yml` only runs the two infra dependencies (Postgres, Redis); there is no `Dockerfile`, `.dockerignore`, or app container definition.

## Practical implication

This repo is at the stage of "the core scoring/planning math has a first draft," not "a service exists that can be deployed, hardened, or load-tested." Any discussion of auth hardening, RLS, encryption-at-rest, billing correctness, or rate-limiting is premature relative to what's on disk — those layers haven't been started, so there is nothing yet to harden. See `OPEN_ENGINEERING_ITEMS.md` for what's actually left, roughly in build order.
