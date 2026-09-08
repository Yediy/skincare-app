# Security and Safety Notes

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA.

Plain statement of the actual security and safety posture after this foundation pass — what's real, what's deliberately scoped out, and why. Not a claim of completeness.

## Authentication

- JWT access tokens (`app/security/tokens.py`), 15-minute expiry, HS256, signed with a `JWT_SECRET` that fails closed at boot if missing (`app/config.py` — `Settings` has no default for it).
- Refresh tokens: opaque random values (`secrets.token_urlsafe(48)`), stored server-side only as SHA-256 hashes, never the raw value. Rotation is transactional (Phase 2): consuming the old token and inserting its successor happen in one explicit Postgres transaction, so a mid-rotation failure rolls back to a coherent, retryable state instead of stranding the user. Replay of an already-consumed token revokes the entire token family, verified with a real forced `UniqueViolationError`, not a mock.
- Account state (`is_active`, `deleted_at`) is checked against Postgres directly on **every** authenticated request (`get_current_user`), not just a Redis TTL artifact — a disabled/deleted account's existing access tokens stop working immediately, verified with a real test that disables an account mid-session and reuses its still-unexpired token.
- Redis unreachability fails closed (`503`), never silently treated as "not revoked" (`401`) — this distinction is deliberately preserved and tested.

## Consent

- `consent_events` is append-only: granting always inserts a new row. The one narrow, deliberate exception is `withdrawn_at`, set on the currently-active row by a withdrawal — never a rewrite of what was actually granted historically.
- `/analyze` requires a valid, current-policy-version consent grant (`REQUIRED_CONSENT_TYPE`/`REQUIRED_POLICY_VERSION` in `app/db/consent_repository.py`) before any CV work happens. An old policy version or a withdrawn grant both correctly deny analysis.

## Safety enforcement (allergies, avoid-ingredients, pregnancy/nursing, sensitive skin)

- `SafetyEngine`/`SafetyDecision` (`app/domain/safety_engine.py`) replaces the old inline, undocumented pregnancy/nursing exclusion. Every priority and product-category candidate gets a real, machine-readable decision recorded in `plan.metadata.safety_decisions`.
- **Real, material limitation, stated plainly**: there is no normalized per-product ingredient catalog in this repository (no `offers` table exists at all). Allergy/avoid-ingredient enforcement operates against `app/domain/product_safety.py`, a **category-level** adapter — a static, hand-authored mapping from the existing `product_categories` strings (e.g. `"retinoid"`, `"cleanser"`) to a representative ingredient/allergen profile for that category. This is not per-SKU enforcement. Once a real product catalog exists, this needs to become a per-product lookup; treat `product_safety.py` as the seam where that swap happens, not as a finished catalog. A missing category profile fails closed (`SAFETY_DATA_UNAVAILABLE`, excluded) rather than silently defaulting to "safe".
- Sensitive skin caps active-ingredient frequency (`SENSITIVE_SKIN_INTENSITY_LIMIT`, currently a hardcoded cap of 2x/week for retinoid-family/acid categories) rather than only appending a disclaimer sentence — the generated routine text itself reflects the real cap, and the disclaimer only claims an adjustment happened when one actually did.
- A beginner's advanced-default concern is no longer dropped outright — it stays visible with a capped `effective_intensity` instead (Phase 15).

## Database isolation (Phase 17 — partial, stated plainly)

- The application's runtime connection (`DATABASE_URL`) now uses a restricted `skincare_app` Postgres role — confirmed via direct `pg_roles` query: `rolsuper=false, rolcreatedb=false, rolcreaterole=false, rolbypassrls=false`. It owns no tables; migrations still run as the `postgres` superuser/owner (`alembic.ini`'s `sqlalchemy.url` is unchanged).
- Row-level security is enabled on **`user_profiles` and `consent_events` only**, via `SET LOCAL`-scoped (`set_config(..., true)`) `app.current_user_id` context set as the first statement of every transaction that touches those tables (`app/db/profile_repository.py`, `app/db/consent_repository.py`).
- **`users` and `refresh_tokens` are explicitly, deliberately out of RLS scope in this pass.** `users` needs a pre-authentication lookup by email during `/login`, before any session identity exists to scope RLS by. `refresh_tokens` is looked up by an unguessable per-row secret (the token hash), not by session identity — neither fits the `user_id = current_setting(...)` isolation model without a materially different design. This is a real, tracked gap, not a silent omission — see `OPEN_ENGINEERING_ITEMS.md`.
- Cross-user isolation was verified with real PostgreSQL integration tests run through the restricted role (not the superuser, not mocked): User A cannot read/update/delete User B's `user_profiles`/`consent_events` rows even when explicitly querying by User B's ID. [Pooled-connection-reuse-does-not-leak-context test result: see FOUNDATION_IMPLEMENTATION_REPORT.md for the final pass/fail state and root cause if it failed.]

## Capture quality and CV confidence (Phases 7-11)

- Real head pose (`app/cv/head_pose.py`, `cv2.solvePnP`) and a structured `CaptureAssessment` (`app/cv/capture_assessment.py`, PASS/BORDERLINE/FAIL) now gate `/analyze`: a `FAIL` capture blocks metric computation entirely before any CV math runs on it, rather than silently producing a plan from a bad photo.
- Every one of the 8 skin/face metrics is returned as a `MetricResult` with its own `confidence` and `status` (`app/cv/metric_result.py`, `app/cv/metric_confidence.py`) — an `ABSTAINED` metric (`value=None`) is provably excluded from triggering a personalized concern in `FacialScorer` (`app/ml/scorer.py`), verified with a real test proving this holds regardless of what the withheld underlying value implied.
- **This is not clinical validation.** `calibration_version` is literally `"uncalibrated-1.0"` on every result; see `CV_VALIDATION_LIMITATIONS.md` for the full, honest breakdown of what's measured, what corrupts it, and what remains unsolved (the texture/blur circular dependency, most notably).

## What's explicitly NOT done (see CV_VALIDATION_LIMITATIONS.md and OPEN_ENGINEERING_ITEMS.md for full detail)

- No billing/subscription/webhook code of any kind exists in this repository.
- No rate limiting or quota system of any kind exists in this repository.
- No offer/product catalog exists — safety enforcement is necessarily category-level, not per-product, as stated above.
