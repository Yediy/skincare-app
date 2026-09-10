# Usage and Rate Limit Architecture

Two independent, deliberately separate mechanisms built this pass, both
Redis/Postgres-native (no new infrastructure dependency):

1. **Rate limiting** (`app/middleware/rate_limiter.py`) — protects against
   abusive request *bursts*, keyed by identity (IP or user), reset on a
   fixed time window. Answers "is this identity calling too fast?"
2. **Usage/quota reservation** (`app/db/usage_repository.py`,
   `app/domain/entitlement.py`) — governs how many *analyses* a user may
   run per billing period, independent of any payment provider. Answers
   "does this user have allowance left this period?"

A request can be within its rate limit and still be quota-denied, or vice
versa — they gate different things and are checked independently.

## Rate limiting — `VERIFIED_IMPLEMENTED`

### Algorithm

A single Lua script (`app/middleware/rate_limiter.py`'s `_RATE_LIMIT_LUA`)
does `INCR` + conditional `EXPIRE` + threshold check as one atomic Redis
operation — not the classic non-atomic `count = GET key; if count < limit:
INCR key`, which races: two concurrent requests can both read the same
under-limit count before either writes, and both proceed, letting the
limit be exceeded under real concurrent load. Proven, not just asserted, by
`tests/middleware/test_rate_limiter.py::
test_concurrent_requests_at_exact_limit_no_overrun`: a real 20-request
concurrent burst against a limit of 5 allows exactly 5 through, never more.

**Fixed window, not sliding window or token bucket** — a deliberate,
documented choice. A fixed window can allow up to ~2x the configured limit
across a single window-boundary instant in the worst case, but needs no
extra bookkeeping beyond one `INCR`-able key per identity+policy+window and
is atomic and correct within each window by construction. Sufficient for
this pass's actual threat model (abusive bursts, credential stuffing,
analysis-endpoint cost control) — not a precision rate-limiting SLA. A
sliding-window log or token-bucket implementation would be a reasonable
future upgrade if the boundary behavior ever actually matters.

### Policies

| Policy | Default limit | Keyed by | Applied to |
|---|---|---|---|
| `auth` | 10 / 60s | client IP | `/signup`, `/login`, `/refresh`, `/logout` (all pre-identity — no user_id exists yet) |
| `analysis` | 20 / 3600s | `user_id` | `/analyze` |
| `general` | 120 / 60s | `user_id` | `/profile`, `/consent`, `/consent/withdraw`, `/me`, `/logout-all` |

All six numbers are `Settings` fields (`RATE_LIMIT_*_MAX`/`RATE_LIMIT_*_
WINDOW_SECONDS`, see `.env.example`), overridable per-deployment without a
code change. `/health/live` and `/health/ready` are deliberately never
rate-limited (probes must not be throttled), and no blanket global limiter
covers every route — each policy is wired explicitly to the routes it
actually applies to (`app/main.py`), so a future webhook endpoint won't
inherit a limiter tuned for a completely different traffic shape by
accident.

### Trusted proxy / X-Forwarded-For handling

`resolve_client_ip()` uses the direct TCP peer (`request.client.host`) by
default. `X-Forwarded-For` is honored **only** when that direct peer is
itself in the configured `TRUSTED_PROXIES` list — otherwise any client
could simply set that header to an arbitrary value (including another
real user's IP, or a different value on every request) and evade IP-based
limiting entirely. Empty/unset (the default) means only the direct peer is
ever trusted, which is always correct even if less useful behind an
undeclared reverse proxy. Verified directly:
`tests/middleware/test_rate_limiter.py::
test_resolve_client_ip_ignores_xff_from_untrusted_peer_even_when_some_proxy_is_trusted`.

### Rate limit failure policy — `VERIFIED_IMPLEMENTED`, per-policy, not blanket

Each `RateLimitPolicy` carries its own `fail_open: bool`, not one
repository-wide choice:

- **`auth` and `analysis` fail closed** (`fail_open=False`): if Redis is
  unreachable, the request is rejected with `503`, never let through
  unverified. Auth endpoints are the highest-value abuse target
  (credential stuffing); analysis is the most expensive endpoint to run
  (real CV compute) — both are conservative by design, matching this
  pass's brief ("for expensive analysis endpoints, prefer conservative
  behavior").
- **`general` fails open** (`fail_open=True`): a Redis blip degrades rate
  limiting for low-risk authenticated reads/writes rather than taking the
  whole API down over a transient cache outage — matching the brief's "for
  ordinary low-risk reads, limited degradation may be acceptable."

Both directions are tested with a real unreachable Redis target (not
mocked): `tests/middleware/test_rate_limiter.py::
test_fail_open_policy_allows_when_redis_unreachable` /
`::test_fail_closed_policy_raises_when_redis_unreachable`.

### Response contract

A denied request gets `429 Too Many Requests` with a `Retry-After` header
(seconds until the current window's key expires) — not just a bare
rejection. A fail-closed Redis outage gets `503`.

## Usage/quota reservation — `VERIFIED_IMPLEMENTED`

### Lifecycle

```text
request arrives
    ↓
idempotency check (by request_id, inside the same locked section as below)
    ↓
reserve quota (atomic count-against-allowance check + INSERT)
    ↓
analysis proceeds
    ↓
success  → consume reservation
failure  → release reservation (frees the slot; does not retroactively un-deny others)
```

Implemented across two layers:

- `app/db/usage_repository.py` — the atomic Postgres primitives
  (`reserve`/`consume`/`release`) against `analysis_usage` (migration
  `ee276e90a60f`).
- `app/domain/entitlement.py` — `EntitlementService` (how much allowance)
  composed with `UsagePolicyService` (the actual reserve/consume/release
  orchestration), the RevenueCat-independent boundary (see "Entitlement
  abstraction" below).

`app/domain/analysis_service.py::perform_analysis` wires this into the real
request lifecycle: consent check first (a request rejected for missing
consent never touches quota), then reservation, then the CV/scoring/plan
sequence wrapped so **any** failure past the reservation point releases the
slot rather than consuming it — a failed attempt produced no usable result,
so it should not count against the user's allowance. Only full success
consumes it. Proven end-to-end (not just at the repository layer) by
`tests/domain/test_analysis_service.py::
test_perform_analysis_releases_reservation_when_no_face_detected` and
`::test_perform_analysis_consumes_reservation_on_success`.

### Atomicity — the actual concurrency guarantee

`reserve()` takes a Postgres advisory transaction lock
(`pg_advisory_xact_lock`, keyed by `hashtextextended(user_id||period_key)`)
**before** doing anything else — every concurrent reservation attempt for
the same `(user_id, period_key)` is fully serialized through that one line,
so the idempotency check and the quota-count-then-insert that follow are
never racing another concurrent attempt for the same user+period. The lock
releases automatically at transaction end (commit or rollback) — no manual
unlock, nothing held across requests, no separate counter table to keep in
sync.

Proven, not just asserted, with a real concurrent race:
`tests/usage/test_usage_repository.py::
test_concurrent_reservations_never_exceed_allowance` — allowance `3`, 10
simultaneous unique `request_id`s, `asyncio.gather`'d — asserts **exactly**
3 succeed and 7 are denied. Not 4. Not 7.

### Idempotency

`request_id` is globally `UNIQUE` at the schema level. `reserve()` checks
for an existing row by `request_id` **inside** the locked section (not
before acquiring the lock — acquiring first is what prevents two
concurrent callers with the *same* request_id from both passing a
pre-lock existence check and then each attempting an insert). Matches the
exact idempotency philosophy already established by the `jobs` table
(`app/queue/postgres_queue.py`): one `request_id` maps to one row forever,
**including after a `RELEASE`** — a retry with that same `request_id`
returns the existing (now-`RELEASED`) row, not a fresh reservation; a
genuinely new attempt requires a new `request_id`. A defensive
`UniqueViolationError` fallback (mirroring `PostgresJobQueue.enqueue`)
covers the one edge case the advisory lock alone doesn't serialize: the
same `request_id` reused under a *different* `period_key` (e.g. a retry
landing just after a month boundary).

Proven with a real concurrent race:
`tests/usage/test_usage_repository.py::
test_concurrent_identical_request_id_produces_exactly_one_reservation` — 10
concurrent calls with the identical `request_id` produce exactly one row,
not ten, and no `UniqueViolationError` reaches any caller.

### Entitlement abstraction — `VERIFIED_IMPLEMENTED`, RevenueCat NOT integrated

`EntitlementService` (abstract: "what allowance does this user have?") and
`UsagePolicyService` (the reserve/consume/release orchestration built on
top of it) are the seam RevenueCat will plug into later — business logic
asks this interface, never RevenueCat directly.
`FreeTierEntitlementService` (a fixed monthly allowance, default 3) is a
**real, working implementation used by every environment today, including
production** — not a test-only mock or a `NotImplementedError` stub.
Swapping in a billing-aware `EntitlementService` later requires no change
to `perform_analysis`, `/analyze`, or any other caller of this interface.

Period granularity is calendar-month, UTC (`current_period_key()`,
`"2026-09"`-shaped) — the only granularity this pass implements, and a
`UsagePolicyService`-layer decision, not an `analysis_usage` schema one
(`period_key` is just a string).

### Row-level security

`analysis_usage` is user-owned data (unlike the catalog tables in
`PRODUCT_CATALOG_ARCHITECTURE.md`), so it uses the same RLS pattern as
`user_profiles`/`consent_events`: `user_id = current_setting('app.
current_user_id')`, `SET LOCAL`-scoped as the first statement of every
transaction that touches it. Verified with real cross-user isolation
tests, not mocked: `tests/database/test_product_usage_rls.py::
test_user_a_cannot_read_user_b_analysis_usage` /
`::test_user_a_cannot_release_user_bs_reservation`.

## What remains partial or not built

- No HTTP-visible "remaining allowance" endpoint — a client currently only
  learns it's out of quota by getting a `429` from `/analyze` itself.
- `analysis_usage` rows are never pruned/archived — an append-mostly audit
  trail by design (Phase 7's own description), but a real retention policy
  is undecided and undocumented; not a concern at this pass's scale.
- RevenueCat itself is explicitly not integrated this pass, per the
  brief's own instruction — `EntitlementService`/`UsagePolicyService` exist
  precisely so that integration doesn't require touching the domain layer
  when it happens.
