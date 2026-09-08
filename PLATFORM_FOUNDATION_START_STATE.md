# Platform Foundation — Start State

**Commit this document describes:** `a234918` (branch `master`, working tree clean at time of writing, verified with `git status --short`).

Phase 0 of the "Production Platform Foundation and Billion-Scale Readiness" pass. Every row below was checked directly against this commit — file existence, grep for the relevant symbol, or (where marked) a real test run — not carried over from any prior document's claim.

| Item | Status | Evidence |
|---|---|---|
| Tests | VERIFIED | `backend/tests/` — 24 test files across `auth/`, `cv/`, `database/`, `integration/`, `planning/`, `unit/`. |
| CI | VERIFIED | `.github/workflows/ci.yml` — real Postgres 15 + Redis 7 service containers, runs Alembic migrations to head, then `pytest tests/ -v` on every push/PR to `master`/`main`. |
| Transactional refresh rotation | VERIFIED | `app/main.py` `/refresh` — old-token consumption and successor insert in one explicit transaction; `tests/auth/test_refresh_rotation.py` (6 tests, including a real forced `UniqueViolationError`, not mocked). |
| Account invalidation | VERIFIED | `app/security/auth.py` `get_current_user` checks `users.is_active`/`deleted_at` against Postgres on every request, not just Redis TTL; `tests/auth/test_account_invalidation.py`. |
| Profile persistence | VERIFIED | `app/db/profile_repository.py`, `tests/planning/test_profile_persistence.py`. |
| Consent ledger | VERIFIED | `app/db/consent_repository.py` (append-only, `withdrawn_at` the one exception), gates `/analyze`; `tests/auth/test_consent.py`. |
| Supplement removal | VERIFIED | `grep -rn "supplement" app/` returns zero matches — no supplement-recommendation code exists anywhere in the current tree; `tests/planning/test_supplement_removal.py` covers the negative case. |
| CaptureAssessment | VERIFIED | `app/cv/capture_assessment.py` (PASS/BORDERLINE/FAIL); `tests/cv/test_capture_assessment.py`. |
| Head pose | VERIFIED | `app/cv/head_pose.py` (`cv2.solvePnP`); `tests/cv/test_head_pose.py`. |
| All 8 `MetricResult` implementations | VERIFIED | `app/cv/metric_result.py` (`MetricResult` dataclass, `MetricStatus` enum), wired through `app/cv/metric_extractors.py` and `app/cv/pipeline.py`. |
| Abstention | VERIFIED | `app/cv/metric_confidence.py`, `app/ml/scorer.py`; `tests/cv/test_scorer_abstention.py`. |
| SafetyEngine | VERIFIED | `app/domain/safety_engine.py::SafetyEngine`; `tests/planning/test_safety_engine.py`, `tests/planning/test_safety_enforcement_in_plan.py`. |
| SafetyDecision | VERIFIED | `app/domain/safety_engine.py::SafetyDecision` (dataclass), recorded in `plan.metadata.safety_decisions`. |
| Restricted database runtime role | VERIFIED | Migration `7b38b717546e` creates `skincare_app` (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`); `.env`'s `DATABASE_URL` and `tests/conftest.py`'s `APP_DATABASE_URL` both connect as this role, not the superuser. |
| Current RLS behavior | VERIFIED | All four application tables (`users`, `refresh_tokens`, `user_profiles`, `consent_events`) have RLS enabled as of migration `feb038fd05bd` (P0-1, committed `c11d86e`/`a234918`, just prior to this pass) — `users`/`refresh_tokens` via a `SECURITY DEFINER` login-lookup function and a `token_hash`-scoped GUC respectively, the other two via the plain `user_id`-scoped GUC. 14 real cross-user isolation tests in `tests/database/test_rls_isolation.py`, run through the restricted role.

## What was explicitly NOT reopened

Per this pass's own instruction not to reopen already-correct systems: the RLS/auth work above (`users`/`refresh_tokens` isolation) was verified as already complete and correct, not rewritten, not given a competing migration, and not altered. Migration `feb038fd05bd` is the current head; this pass's own migrations, if any, will chain from it.

## What this document does not cover

Everything from Phase 1 onward in this pass's brief (test determinism investigation, containerization, health/readiness, production config validation, secrets scanning, object storage abstraction, connection pooling hardening, backups, async analysis boundaries, job queue, idempotency, rate limiting, usage ledger, product/ingredient catalog, safety-engine SKU migration, commercial firewall, billing seam, cell readiness, outbox, analytics separation, observability, structured logging, CI/supply-chain hardening, deployment docs, topology, failure domains, scaling triggers) is **NOT_IMPLEMENTED** as of this commit — this document is the starting line for that work, not a status report on it.
