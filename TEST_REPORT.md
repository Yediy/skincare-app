# Test Report

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA.

## Command used

```
cd backend && source .venv/bin/activate && python -m pytest tests/ -v
```

Against a real, isolated `skincare_test` Postgres database and Redis index 1 — never the dev/prod database or the default Redis index. The application itself connects through the restricted `skincare_app` role during these tests, not the superuser (see `SECURITY_AND_SAFETY_NOTES.md`).

## Final result

Full suite, one complete run (this sandbox's system load average was ~11 during this run):

```
1 failed, 79 passed, 8 warnings, 2 errors in 5050.70s (1:24:10)
```

All three non-passing results are `redis.exceptions.TimeoutError` (a Redis *connection* timeout, not an assertion failure or logic error) — one at test setup (`tests/cv/test_head_pose.py::test_yawed_face_yields_larger_yaw_magnitude_than_frontal`, which touches Redis only via an autouse fixture, not its own logic), one at teardown, and one manifesting as the app's own documented fail-closed behavior (`assert 503 == 200` in `test_poor_lighting_causes_a_color_metric_to_abstain` — Redis was genuinely unreachable for a moment, and the app correctly returned `503` rather than silently treating "unreachable" as "not revoked"). This matches a pattern of transient, load-related Redis timeouts observed and confirmed multiple times earlier in this same session on this small shared VM.

Both affected test files were re-run in isolation afterward to check for a real bug rather than assuming transience:

```
tests/integration/test_end_to_end_analysis.py -v   → 7 passed in 796.76s (0:13:16)
tests/cv/test_head_pose.py -v                        → 3 passed in 239.99s (0:03:59)
```

Every test that failed or errored in the full run passed cleanly on retest, with no code changes in between — confirming environmental (Redis-connection) flakiness under sandbox load, not a code defect. No test failed on logic/assertion grounds in this pass.

## Breakdown by directory

| Directory | Tests | Notes |
|---|---|---|
| `tests/unit/` | 2 | App import + health endpoint |
| `tests/database/` | 8 | Infra smoke tests (4) + RLS cross-user isolation (4) |
| `tests/auth/` | 14 | Account invalidation (3), consent (5), refresh rotation (6, incl. concurrent + forced-rollback) |
| `tests/integration/` | 7 | Full chain through real CV pipeline, consent gating, no-face-detected, excessive yaw → FAIL, moderate yaw → BORDERLINE, poor lighting → metric abstains, scorer-abstention cross-reference — all real, none skipped |
| `tests/planning/` | 25 | SafetyEngine (10), safety-in-plan integration (5), intensity separation (3), ranking correction (2), profile persistence (3), supplement removal (2) |
| `tests/cv/` | 25 | Head pose (3), capture assessment (4), metric confidence (13), scorer abstention (5) |

2 + 8 + 14 + 7 + 25 + 25 = 81, exactly matching pytest's `collected 81 items`. The full run's `1 failed + 79 passed + 2 errors = 82` double-counts one test (`test_poor_lighting_causes_a_color_metric_to_abstain`, which failed at call and then errored again at teardown — one test item, two reported outcomes), consistent with both outcomes tracing to the same single Redis-timeout episode.

## No tests are skipped

The three scenarios that were previously stubbed with `pytest.mark.skip` (blocked on Phases 7-11) are now real, executing tests — see `test_end_to_end_analysis.py` above. There are zero `skip` markers left anywhere in `tests/`.

## Notable things proven by real execution, not just code review

- A real forced `asyncpg.exceptions.UniqueViolationError` (via a deliberately colliding token hash) proves the transactional refresh-rotation rollback actually works, not just that the code compiles.
- A real concurrent `asyncio.gather` of two simultaneous `/refresh` calls against the same token proves exactly one wins, not two.
- A real disabled-account test proves an outstanding access token stops working mid-session, not just at natural expiry.
- Real cross-user Postgres queries through the actual restricted `skincare_app` role (not the superuser, not mocked) prove RLS isolation — including a genuine bug caught and fixed during this pass (a custom GUC reverting to `''` rather than `NULL` after a transaction, causing a cast error rather than clean isolation; fixed with `NULLIF`).
- A real photo (`grace_hopper.jpg`, bundled with `matplotlib`'s sample data) run through the actual MediaPipe pipeline, actual scorer, actual `SafetyEngine`, actual `PlanService`, over actual HTTP, against a real database, proves the full `/analyze` chain genuinely works end to end — not asserted from reading the code.

## Environmental note (does not affect correctness, worth knowing)

Test wall-clock time in this specific sandboxed environment was substantially slower than test count alone would suggest — a system-wide load average around 11 (on a small, memory-constrained shared VM) was traced mid-pass to an old, unrelated `uvicorn --reload` dev server process that had been running continuously and accumulating CPU time; killing it roughly halved subsequent run times. This is an artifact of the development sandbox, not of the test suite or application code, and would not be expected to reproduce in CI's dedicated runners.
