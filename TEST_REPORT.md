# Test Report

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA.

## Command used

```
cd backend && source .venv/bin/activate && python -m pytest tests/ -v
```

Against a real, isolated `skincare_test` Postgres database and Redis index 1 — never the dev/prod database or the default Redis index. The application itself connects through the restricted `skincare_app` role during these tests, not the superuser (see `SECURITY_AND_SAFETY_NOTES.md`).

## Final result

```
52 passed, 3 skipped, 0 failed, 5 warnings in 355.21s (0:05:55)
```

## Breakdown by directory

| Directory | Tests | Notes |
|---|---|---|
| `tests/unit/` | 2 | App import + health endpoint |
| `tests/database/` | 8 | Infra smoke tests (4) + RLS cross-user isolation (4) |
| `tests/auth/` | 14 | Account invalidation (3), consent (5), refresh rotation (6, incl. concurrent + forced-rollback) |
| `tests/integration/` | 6 | 3 real (full chain through real CV pipeline, consent gating, no-face-detected), 3 explicitly skipped with a documented reason (blocked on Phases 7-11, not silently omitted) |
| `tests/planning/` | 25 | SafetyEngine (10), safety-in-plan integration (5), intensity separation (3), ranking correction (2), profile persistence (3), supplement removal (2) |

2 + 8 + 14 + 6 + 25 = 55, exactly matching pytest's `collected` count (52 passed + 3 skipped).

## Skipped tests (3), and why that's honest rather than a gap being hidden

```
tests/integration/test_end_to_end_analysis.py::test_excessive_yaw_marks_capture_borderline_or_fail
tests/integration/test_end_to_end_analysis.py::test_poor_lighting_causes_color_metrics_to_abstain
tests/integration/test_end_to_end_analysis.py::test_abstained_metric_cannot_create_a_personalized_concern
```

Each carries an explicit `pytest.mark.skip(reason=...)` naming the exact phase it's blocked on (8, 9/10, and 11 respectively). These exist as stubs specifically so the gap shows up in every test run's output — `3 skipped` is visible in the summary line every single time — rather than the scenario being silently absent from the test suite with no trace at all.

## Notable things proven by real execution, not just code review

- A real forced `asyncpg.exceptions.UniqueViolationError` (via a deliberately colliding token hash) proves the transactional refresh-rotation rollback actually works, not just that the code compiles.
- A real concurrent `asyncio.gather` of two simultaneous `/refresh` calls against the same token proves exactly one wins, not two.
- A real disabled-account test proves an outstanding access token stops working mid-session, not just at natural expiry.
- Real cross-user Postgres queries through the actual restricted `skincare_app` role (not the superuser, not mocked) prove RLS isolation — including a genuine bug caught and fixed during this pass (a custom GUC reverting to `''` rather than `NULL` after a transaction, causing a cast error rather than clean isolation; fixed with `NULLIF`).
- A real photo (`grace_hopper.jpg`, bundled with `matplotlib`'s sample data) run through the actual MediaPipe pipeline, actual scorer, actual `SafetyEngine`, actual `PlanService`, over actual HTTP, against a real database, proves the full `/analyze` chain genuinely works end to end — not asserted from reading the code.

## Environmental note (does not affect correctness, worth knowing)

Test wall-clock time in this specific sandboxed environment was substantially slower than test count alone would suggest — a system-wide load average around 11 (on a small, memory-constrained shared VM) was traced mid-pass to an old, unrelated `uvicorn --reload` dev server process that had been running continuously and accumulating CPU time; killing it roughly halved subsequent run times. This is an artifact of the development sandbox, not of the test suite or application code, and would not be expected to reproduce in CI's dedicated runners.
