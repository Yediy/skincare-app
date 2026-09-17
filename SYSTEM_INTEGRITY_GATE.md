# System Integrity Gate V1

**Branch:** `fix/system-integrity-gate-v1`, based on Mobile V1 Phase C1
(`feat/mobile-v1-recommendation-experience`, PR #9).

This is a stabilization pass, not a feature phase. It closes confirmed
shared-system integrity defects in the job queue, the analysis
execution/state machine, and auth revocation authority — required
before Mobile C2 introduces native RevenueCat billing, so billing work
lands on a queue/execution/auth substrate that is actually race-safe.

Documents only what this pass actually implements and tests. For
everything else, see `ARCHITECTURE_CURRENT.md` / `OPEN_ENGINEERING_ITEMS.md`.

## 1. Job-queue lease fencing

**Defect closed:** `jobs.id` + `status = 'claimed'` alone never proved
*current* ownership — a worker whose lease expired and was reclaimed by
a second worker could still call `extend_visibility()`/`acknowledge()`/
`fail()` and have it silently succeed.

**Fix:** migration `44a74f2a79a7` adds `jobs.claim_token UUID`. Every
successful claim/reclaim (`PostgresJobQueue.claim`) mints a fresh
`gen_random_uuid()` atomically in the same `UPDATE`. `acknowledge()`,
`fail()`, and `extend_visibility()` (`app/queue/base.py`,
`app/queue/postgres_queue.py`) now all require the caller's
`claim_token` to match the row's current one; a mismatch (job still
claimed, but by a newer token) raises `JobLeaseLostError`, distinct
from `JobNotFoundError` (job doesn't exist, or isn't claimed by
anyone). `claim_token` is cleared whenever a job returns to `pending`
or reaches a terminal state. Both `app/workers/analysis_worker.py` and
`app/workers/revenuecat_webhook_worker.py` thread the claimed `Job`'s
`claim_token` through every subsequent call and treat
`JobLeaseLostError` as STALE ATTEMPT / ABANDON — no retry, no dead
letter, no compensation.

**Tested:** `tests/queue/test_postgres_job_queue.py` (real Postgres,
real concurrent-claim races) — `test_reclaim_mints_a_new_token_and_stale_calls_are_rejected`
runs the full required sequence (enqueue → A claims → expire → B
reclaims → assert tokens differ → A's heartbeat/acknowledge/retryable-fail/
terminal-fail all rejected → B still owns the job → B acknowledges
successfully). Existing RLS/job-type isolation between `analysis` and
`revenuecat_webhook` (migration `4e5cda3a6bb0`) is unaffected —
`tests/database/test_revenuecat_jobs_isolation.py` runs unmodified
against the new token-aware interface.

## 2. Execution-level lease fencing (analysis)

**Defect closed:** queue fencing alone cannot protect a worker already
inside synchronous CV compute when its queue lease expires and a
second worker legitimately reclaims the job — the first worker could
still commit a result, mark the request FAILED, release quota, or
delete the image out from under the second worker's still-valid
execution.

**Fix:** the same migration adds `analysis_requests.processing_claim_token
UUID`. `mark_processing()` (`app/db/analysis_repository.py`)
unconditionally installs the caller's current claim token whenever a
request enters or re-enters `PROCESSING` (QUEUED→PROCESSING, or a
legitimate PROCESSING→PROCESSING re-entry). `commit_analysis_result()`
and `mark_failed()` both require that token to still match before
doing anything — `commit_analysis_result()` raises
`AnalysisResultCommitFencedError` (translated by
`AnalysisExecutionService.execute()` into `AnalysisExecutionLeaseLostError`)
and persists nothing; `mark_failed()` returns `False` without applying
its `UPDATE`, and `AnalysisExecutionService.mark_terminal_failure()`
checks that *before* releasing the usage reservation or deleting the
image, so a stale worker's terminal-failure attempt performs zero
compensation. `app/workers/analysis_worker.py` treats
`AnalysisExecutionLeaseLostError` the same way as a queue-level
`JobLeaseLostError`: STALE ATTEMPT / ABANDON.

**Tested:** `tests/domain/test_analysis_execution_lease_fencing.py` —
drives worker A's and worker B's tokens directly through the
repository (no mocking) to prove both required races: (1) A finishes
compute after B has reclaimed → A's commit is rejected, B's own commit
then succeeds normally, and (2) A processing → B reclaims → B
completes → A's terminal-failure attempt is a no-op — the request
stays COMPLETED, the result stays present, quota stays CONSUMED.
`tests/domain/test_analysis_execution_service.py` covers the CV
executor lifecycle (section 5) in the same file.

## 3. Analysis status-transition monotonicity

**Defect closed:** repository methods updated `analysis_requests.status`
by id with no constraint on the previous status — nothing but
application convention stopped a stale write from moving a terminal
(COMPLETED/FAILED/CANCELLED) request to any other status.

**Fix:** the same migration adds a `BEFORE UPDATE` trigger
(`enforce_analysis_request_status_transition`) enforcing RECEIVED →
QUEUED → PROCESSING → COMPLETED, with FAILED/CANCELLED reachable from
any non-terminal state, PROCESSING → PROCESSING allowed (re-entrant
claim), and every terminal state permanently closed to further
transitions — enforced by Postgres itself, not just Python discipline.
Repository methods additionally use guarded SQL (`mark_processing`:
only QUEUED/PROCESSING; `mark_failed`: only non-terminal states, fenced
by claim token; `commit_analysis_result`: only PROCESSING, fenced by
claim token). The completed-result transaction still commits
measurements + product recommendations + COMPLETED status + CONSUMED
quota atomically, unchanged from Part VI, Phase 30.

**Tested:** `tests/database/test_analysis_request_status_transitions.py`
proves every required impossible transition
(COMPLETED→PROCESSING/FAILED, FAILED→PROCESSING/COMPLETED,
CANCELLED→PROCESSING/COMPLETED) raises a real `CheckViolationError` at
the database level, plus every documented valid transition and the
PROCESSING→PROCESSING/terminal-same-status idempotent cases.
`tests/domain/test_analysis_execution_lease_fencing.py` covers the
cross-layer race (section 2, same file).

## 4. Postgres as the durable auth revocation authority

**Defect closed:** `get_current_user` (`app/security/auth.py`)
returned `503` the instant Redis was unreachable, even for a
perfectly valid, unrevoked session — access-token revocation was
effectively gated by Redis availability, not by durable state.

**Fix:** Redis remains a fast-deny cache (checked first, when
reachable, purely to skip a DB round trip for the known-revoked case);
a `RedisError` now falls through instead of failing closed.
`get_current_user`'s single authoritative Postgres transaction checks
user existence/`is_active`/`deleted_at`, and — using the existing
`refresh_tokens` schema rather than a new table — that the JWT's
`family_id` both belongs to this user and still has at least one row
with `revoked_at IS NULL` (an unrevoked durable session
representation). `/logout`, `/logout-all`, and `/refresh`'s
replay-detection path all commit their Postgres revocation first and
treat the Redis marker write as best-effort (wrapped in
`try/except RedisError`, logged, never surfaced as a request failure);
`/logout-all` was also reordered (Postgres now runs before the Redis
write, not after). `/me` (DELETE)'s Redis write got the same
try/except treatment; its Postgres-first ordering was already correct.

**Tested:** `tests/auth/test_postgres_revocation_authority.py` —
logout/logout-all/refresh-replay/account-delete each proven durable
under a real Redis-write failure (a proxy that fails only the
propagation write, not the whole client, for the two endpoints gated
by the rate limiter's deliberate fail-closed `AUTH_POLICY` — see that
file's own docstring for why); a fully unreachable Redis proven to
still allow an ordinary valid session through; a token whose
`family_id` belongs to a different user, and one with a wholly
fabricated `family_id`, both proven rejected. Existing
`tests/auth/test_refresh_rotation.py` / `test_account_invalidation.py`
pass unmodified. Refresh-token rotation, replay detection, and RLS are
unchanged.

## 5. CV executor lifecycle

**Fix:** `AnalysisExecutionService` (`app/domain/analysis_execution_service.py`)
now tracks whether it created its own `ThreadPoolExecutor` or received
one via `cv_executor=`. `close()` shuts down only an executor it
created itself, never an injected one. `app/workers/analysis_worker.py`'s
`_main()` installs a SIGTERM/SIGINT handler that lets `run_forever`
finish its current job and return, then calls `close()` before closing
the database pool.

**Tested:** `tests/domain/test_analysis_execution_service.py` —
`test_close_shuts_down_an_executor_it_created_itself` /
`test_close_does_not_shut_down_an_injected_executor`.

## 6/7. Mobile runtime pin and CI gates

**Fix:** `.nvmrc` (`22`) at the repository root and
`mobile/package.json`'s `engines.node` (`>=22 <23`) give a freshly
rebuilt environment one obvious source for the Node version, instead
of depending on someone remembering CI's own pin. CI's `mobile-test`
job gained two real (non-`continue-on-error`) gates after the existing
typecheck/lint/Jest steps: `npx expo-doctor` and
`npx expo export --platform web`. `expo-doctor` found a real,
SDK-57-compatible patch-version drift (`expo` 57.0.22→57.0.23,
`expo-image-manipulator` 57.0.17→57.0.18) — fixed with those exact
patch bumps; Expo and Expo Router were not touched. The existing
production-dependency `npm audit --omit=dev --audit-level=high` gate
is unchanged and still passing.

## Explicitly not touched by this pass

- **Device verification** (physical iOS/Android build/run of the
  mobile app against these backend changes) is still pending — this
  pass validated the mobile CI gates and local export, not a device or
  simulator run.
- **RevenueCat mobile C2** (native SDK, paywall, purchase flow) is not
  started. No `react-native-purchases`, `react-native-purchases-ui`,
  or `expo-dev-client` package was installed. No paywall exists.
- **C3/history/progress** is not started.
- The recommendation/safety model, RLS policies (other than the new
  status-transition trigger, which is orthogonal to RLS), and catalog
  publication gates are unmodified — see section 9 of this pass's own
  brief and `PRODUCT_RECOMMENDATION_PIPELINE.md` / `SECURITY_AND_SAFETY_NOTES.md`.
- Deployment infrastructure (Dokploy, container orchestration) was not
  touched or re-verified by this pass; nothing here should be read as
  a claim that it was.
