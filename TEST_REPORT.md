# Test Report

**Commit this document describes:** see the commit this pass ends on (search this repo's log for "Production Platform Foundation" to find the range).

## Command used

```
cd backend && source .venv/bin/activate && python -m pytest tests/ -v
```

Against a real, isolated `skincare_test` Postgres database and Redis index 1 — never the dev/prod database or the default Redis index. The application itself connects through the restricted `skincare_app` role during these tests, not the superuser (see `SECURITY_AND_SAFETY_NOTES.md`).

## Final result

Full suite, one complete, isolated run (no concurrent pytest process, no concurrent CPU-heavy job on the same box):

```
114 passed, 9 warnings in 770.88s (0:12:50)
```

**Zero failures, zero errors.** This is the deterministic, single-execution, all-green result this pass's Phase 1 set out to reach.

## Phase 1 investigation: making the suite deterministic

The prior pass's `TEST_REPORT.md` recorded transient `redis.exceptions.TimeoutError` failures under this sandbox's system load. This pass investigated rather than re-asserting "environment problem" as a permanent excuse:

1. **Explicit Redis timeouts, everywhere a client is created** (`app/redis_client.py`'s `init_redis`, `tests/conftest.py`'s `redis_client` fixture): `socket_connect_timeout`/`socket_timeout` were previously left at redis-py's implicit defaults. Under real load (or a busy CPU-bound sandbox running mediapipe/opencv work), an unbounded wait turns a transient hiccup into an unpredictable hang rather than the fast, well-defined failure the app's own 503 fail-closed path already depends on. Now configurable via `Settings` (`redis_connect_timeout_seconds`/`redis_socket_timeout_seconds`, default 5.0/5.0).
2. **A bounded, test-infra-only readiness wait** (`tests/conftest.py`'s `_wait_for_test_infra_ready`, called once by the session-scoped `migrated_test_database` fixture): retries connecting to Postgres/Redis for up to 30s before the session's migration run, so a cold-started local Postgres/Redis is tolerated as setup delay rather than counted as a real test failure. Deliberately synchronous (`psycopg2`/`redis`'s sync client, not `asyncpg`/`redis.asyncio`) — an earlier `asyncio.run()`-based version of this collided with pytest-asyncio's own event loop depending on which test file was collected first, a real bug this pass's own test run caught before it reached CI (see the fix's own commit for detail).
3. **`clean_database` was blanket-`autouse=True`, depending on `db_pool`** — meaning every test in the suite, including pure CV-math and config-validation unit tests that touch neither Postgres nor Redis, paid for a real `asyncpg` pool creation and a `TRUNCATE` before every test. Narrowed to only truncate when the test actually declared a dependency on `db_pool`/`app_db_pool`/`client`/`app_instance` (the only fixtures that can leave rows behind), using its own one-off connection rather than dynamically resolving another async fixture (a second real pytest-asyncio incompatibility this narrowing surfaced and then avoided).
4. **Investigated whether the CV-only test files (`test_head_pose.py`, `test_capture_assessment.py`, etc.) could plausibly hit a Redis timeout at all** — confirmed by direct inspection that none of them request the `client`/`redis_client` fixtures, so a Redis-attributed failure in those files would have to come from a shared fixture, not the test's own logic. Point 3 above is the concrete fix for exactly this class of unnecessary infra coupling.

**What did NOT reproduce this pass**: two separate full-suite runs during this pass (one mid-pass with earlier code, 90 passed/1 failed — the 1 failure was later proven, by isolated re-run, to be self-inflicted test-runner interference from accidentally running two pytest processes concurrently against the same shared test database, not a Redis timeout; and the final run above, 114/0) recorded zero Redis-connection-timeout failures. The hardening above is real and worth keeping regardless, but this pass cannot honestly claim to have reproduced-then-fixed the original timeout — only to have closed every concrete gap that could cause one, and to have caught two different real bugs (both pytest-asyncio/event-loop interactions) along the way that a less careful "add a retry and move on" pass would have missed.

## Breakdown by directory (114 total, matching pytest's own `collected 114 items`)

| Directory | Tests | Notes |
|---|---|---|
| `tests/unit/` | 14 | App import, liveness (1), readiness incl. simulated DB outage (2), production config validation (10) |
| `tests/storage/` | 11 | `CloudflareR2ObjectStorage` contract via `botocore.stub.Stubber` — no real R2 connectivity |
| `tests/database/` | 18 | Infra smoke (4) + RLS cross-user isolation across all four tables (14) |
| `tests/auth/` | 14 | Account invalidation (3), consent (5), refresh rotation (6) |
| `tests/integration/` | 7 | Full chain through the real CV pipeline, consent gating, no-face-detected, excessive/moderate yaw, poor-lighting abstention |
| `tests/planning/` | 25 | SafetyEngine, safety-in-plan, intensity separation, ranking correction, profile persistence, supplement removal |
| `tests/cv/` | 25 | Head pose, capture assessment, metric confidence, scorer abstention |

No `pytest.mark.skip` markers anywhere in `tests/` — unchanged from before this pass.

## Verified beyond the test suite itself, this pass

- The Docker image (`backend/Dockerfile`) was actually built (`docker build`, ~2.1GB) and run (`docker run`) against the real dev Postgres/Redis containers, not just authored: `/health/live` → `200`, `/health/ready` → `200` with both checks `ok`, a real `POST /signup` round-tripped through the RLS-protected `users` table successfully, and the running process was confirmed as `appuser` (uid 1000), not root.
- Production config rejection was verified against that same real container, not just the unit tests: `docker run` with `ENVIRONMENT=production` plus a short JWT secret, a dev-marker `DATABASE_URL`, and wildcard `ALLOWED_ORIGINS` crashed at startup with an itemized `pydantic` `ValidationError` naming each violation; the same image with safe config started and served traffic normally.

## Notable things proven by real execution (carried forward, still true)

- A real forced `asyncpg.exceptions.UniqueViolationError` proves transactional refresh-rotation rollback actually works.
- A real concurrent `asyncio.gather` of two simultaneous `/refresh` calls proves exactly one wins.
- Real cross-user Postgres queries through the actual restricted `skincare_app` role prove RLS isolation on all four tables, including the login-lookup function's exact-email bypass and the token-hash policy's per-row (not per-family) scoping.
- A real photo run through the actual MediaPipe pipeline, actual scorer, actual `SafetyEngine`, actual `PlanService`, over actual HTTP, against a real database, proves the full `/analyze` chain genuinely works end to end.
