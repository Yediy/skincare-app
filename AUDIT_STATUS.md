# AUDIT_STATUS.md — Skincare Priority Engine

Classification of every P0 release blocker from `07_IMPLEMENTATION_MATRIX.json`
/ `08_MASTER_SINGLE_FILE.md`, against the real repository. Status values per
the bundle's own required list: CONFIRMED, ALREADY_FIXED, NOT_APPLICABLE,
NEEDS_CLARIFICATION, BLOCKED, COMPLETED.

**This is the direct Claude Code confirmation pass required by the bundle's
own rule ("the live repository is the source of truth").** A prior chat
session assembled a first-pass draft from its own record of repo checks;
every P0 item below was independently re-verified against the live
repository — fresh `grep`s, a fresh `alembic history` run, and direct file
reads, not a recall of the earlier record. This file has now been through
**two separate independent confirmation passes** (each re-running every
check from scratch rather than trusting the previous pass's output), and
both agreed on every item with zero corrections. The two
`NEEDS_CLARIFICATION` items on `quota` and `rate_limit` are closed for
real below, with the actual grep output from both passes.

| ID | Title | Status | Evidence |
|---|---|---|---|
| P0-01 | Remove raw-photo AsyncStorage persistence | **NEEDS_CLARIFICATION** | Mobile/React Native code — not in this backend repo. The `mobile/` directory in this repo is empty scaffolding (10 empty subdirectories, 0 files) — there is no mobile codebase here to check `AsyncStorage` usage against. Needs the actual mobile repository, if one exists elsewhere. |
| P0-02 | Fix account-deletion schema/service contradiction | **NOT_APPLICABLE** (as literally described) | `grep -rn "plan_feedback\|offer_clicks"` across all `.py`/`.sql` files → zero matches. The specific `NOT NULL` contradiction described can't exist because neither table exists. Separately unresolved: no account-deletion route or service of any kind exists yet — not the same finding, and worth its own future issue rather than folding into this one. |
| P0-03 | Enforce sensitivity/allergy/avoid-ingredient safety | **CONFIRMED** | No `offers` table (`grep -rn "CREATE TABLE offers" backend/migrations/` → zero matches), no ingredient metadata, no enforcement logic anywhere in `plan_service.py` or elsewhere. `allergies`/`avoid_ingredients` are captured into a constraints dict (`plan_service.py:100-101`) but never read again. |
| P0-04 | Remove automatic supplement recommendations | **CONFIRMED** | `_build_supplement_recommendations()` (`plan_service.py:297-322`, full contents re-read directly) is real and called **unconditionally** at `plan_service.py:34` — no `if` guard around the call — with its result included in every returned plan at `plan_service.py:53`. Generates named supplements with specific dosages (e.g. "Vitamin C (1000mg)", "Iron (if deficient)") from a hardcoded `priority_id -> supplement list` map, keyed purely on photo-derived priorities. The function's only parameter is `priority_ids` — it has no `constraints`/`user_profile` argument at all, so it structurally cannot reference `is_pregnant`, `is_nursing`, or any allergy field even if someone wanted it to (confirmed by grepping the function body directly: zero matches for "pregnan", "nursing", or "allerg"). Disclaimer text, verbatim: *"Consult healthcare provider before starting any supplement regimen. These are suggestions based on common deficiencies related to your priorities."* Live in every `/analyze` response right now. |
| P0-05 | Correct RevenueCat cancellation lifecycle | **NOT_APPLICABLE** | `grep -rin "revenuecat\|webhook\|subscription" backend/app backend/migrations` → zero matches. No billing code exists to contain this bug. |
| P0-06 | Protect subscription state from out-of-order webhooks | **NOT_APPLICABLE** | Same grep, same result — no webhook code exists at all. |
| P0-07 | Make free-tier quota reservation atomic | **NOT_APPLICABLE** | `grep -rin "quota"` across every `.py`/`.sql`/`.yml`/`.ini` file in the repo → zero matches (the only hits anywhere are in this session's own prior audit `.md` files, describing its absence — not in any code). No quota code exists to make atomic or non-atomic. |
| P0-08 | Make Redis rate limiting atomic | **NOT_APPLICABLE** | `grep -rin "rate_limit\|ratelimit\|rate limit"` across every `.py`/`.sql`/`.yml`/`.ini` file → zero matches. `backend/app/middleware/__init__.py` is confirmed empty (0 lines). No rate-limiting code of any kind exists. |
| P0-09 | Fix refresh-token race and replay detection | **COMPLETED** | Built from scratch this session: atomic `UPDATE refresh_tokens SET used_at = now() WHERE used_at IS NULL ... RETURNING` (proven race-safe across two independent concurrent-request runs, with the winning request flipping between runs — confirming it's genuinely DB-arbitrated, not an artifact of request ordering). Family-wide revocation on replay proven directly: a second, freshly-issued access token from the *same family* was also rejected (`"Token has been revoked"`) after a replay was detected, not just the replayed token itself. |
| P0-10 | Immediate session invalidation after disable/delete | **BLOCKED** | The underlying mechanism is itself COMPLETED and proven this session: Redis-backed `revoked_family`/`user_tokens_invalid_before` checks in `get_current_user`, with a verified fail-closed `503` (not `401`) when Redis is unreachable, and verified recovery without an app restart once Redis returns. But there is no account-disable or account-deletion route to invoke it from. Blocked on that feature existing, not on a missing design decision. |
| P0-11 | Enforce RLS with real runtime DB role | **CONFIRMED** | `grep -rin "rolsuper\|rolbypassrls\|verify_rls_safe_role\|ROW LEVEL SECURITY\|CREATE POLICY"` across the whole repo → zero matches. `DATABASE_URL` in the real `backend/.env` connects as `postgres` — the Postgres container's vendor-default superuser — for every query, including this session's newly-created `users`/`refresh_tokens` tables. No restricted application role has ever been created. |
| P0-12 | Perform real encrypted-field migration | **NOT_APPLICABLE** | `alembic history` (run fresh this pass): `<base> -> bd3e8b8e56bf, create users table` → `bd3e8b8e56bf -> 4226bc07fe96, add password_hash to users` → `4226bc07fe96 -> f483e026482a (head), create refresh_tokens table`. Three migrations, ever. No encrypted-field migration of any kind exists to be fake. |
| P0-13 | Repair notification dedup migration | **NOT_APPLICABLE** | `grep -rin "notification"` across `backend/app` → zero matches. No notification-related code anywhere. |
| P0-14 | Align privacy/safety claims with real behavior | **CONFIRMED** | This project directly experienced this exact failure mode twice within this same session: a full auth/refresh-token/safety-engine subsystem was described in prior chat text as already implemented and fixed, then confirmed entirely absent from the real repository on direct inspection — twice, for two different subsystems (an "offer-level SafetyEngine fix" and, separately, this very audit bundle's own draft needing independent re-confirmation rather than being trusted as-is). |

## Summary
- **COMPLETED:** 1 (P0-09)
- **CONFIRMED (real, unfixed):** 3 (P0-03, P0-04, P0-11) — plus P0-14 as the meta-finding this whole process keeps relearning
- **BLOCKED:** 1 (P0-10 — mechanism done, dependent feature missing)
- **NOT_APPLICABLE:** 7 (P0-02, 05, 06, 07, 08, 12, 13 — underlying subsystems don't exist)
- **NEEDS_CLARIFICATION:** 1 (P0-01 — different repo; no mobile codebase present here to check)

## What "NOT_APPLICABLE" actually means here — read before treating this as good news

Seven P0s being NOT_APPLICABLE isn't seven bugs avoided — it's seven entire
subsystems (billing, quota, rate limiting, encryption, notifications, the
specific deletion logic) that were never built in the first place. The
audit assumed a more complete application than exists. Don't let a short
"confirmed" list read as "mostly fine" — it reads as "most of this app
doesn't exist yet," which is a different, larger finding than any individual
bug on this list.

## Additional discovery closed this pass (not separate P0 IDs, but tied to earlier verification findings)

`grep -rn` across `backend/app` for each of the following, individually —
all **DOES NOT EXIST**, zero matches:
- `MetricResult` — no per-metric confidence/abstention result type exists (ties to the original `REPOSITORY_VERIFICATION_REPORT.md` Phase C finding — the 8 CV metrics still return bare floats).
- `CaptureAssessment` — no structured capture-quality result type exists (ties to Phase B — `CaptureQualityAssessor` still returns a single float).
- `solvePnP` — no head-pose estimation exists in any form.
- `head_pose` — same; zero references anywhere.
- `abstention` — no abstention concept exists anywhere in the metric-extraction code.

## Corrections made to the prior draft during this pass

None, across either confirmation pass. Every directly re-verified item
(P0-02 evidence, P0-05/06, migration count/order, P0-11's RLS absence and
`DATABASE_URL` role, P0-13, P0-04's full contents and structural inability
to reference pregnancy/allergy fields) matched the draft's claims exactly
against fresh evidence gathered independently, twice. The two prior
`NEEDS_CLARIFICATION` items (P0-07, P0-08) are closed as `NOT_APPLICABLE`,
based on the `quota` and `rate_limit`/`ratelimit` greps — re-run in full
in this second pass, unrestricted by file extension this time, still
returning zero matches in any actual code, config, or migration file
(every hit either pass was inside this project's own prior audit `.md`
documents, describing the absence, not the absence itself).
