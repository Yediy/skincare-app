# Entitlement State Machine

**Commit this document describes:** the `feat/revenuecat-entitlement-sync` branch, base `master` at `76fcaa2b454ef390a4e50e38160ca5e011448e31`.

The `user_entitlements.status` values and the RevenueCat webhook event that drives each transition. Implemented in `app/domain/revenuecat_entitlement_processor.py`; verified by `tests/domain/test_revenuecat_entitlement_processor.py`. See `REVENUECAT_INTEGRATION_NOTES.md` for the underlying RevenueCat semantics this table is derived from.

## Statuses

| Status | Meaning | Counts as "paid" for `RevenueCatEntitlementService`? |
|---|---|---|
| `ACTIVE` | Currently entitled, in good standing (whether or not auto-renew is on). | Yes |
| `GRACE_PERIOD` | A renewal charge failed, but RevenueCat/the store may still extend access through a grace window. | Yes |
| `EXPIRED` | The paid period genuinely ended. | No |
| `REVOKED` | Access removed immediately — a refund, or a `TRANSFER` moved the entitlement to a different user. | No |

Four values, not a boolean, precisely because `GRACE_PERIOD` is real RevenueCat-documented behavior distinct from both "fully fine" (`ACTIVE`) and "gone" (`EXPIRED`), and `REVOKED` needs to be distinguishable from a natural `EXPIRED` for support/audit purposes even though both currently map to the same "no" allowance answer.

## Transition table

Before any row below applies, a lifecycle event first passes the
**`entitlement_ids` gate**: `settings.revenuecat_entitlement_id in
(event.entitlement_ids or [])` must be true, or the event is marked
`NOT_RELEVANT` and none of `status`/`will_renew`/`expires_at` are
touched (see below). `TRANSFER` is not lifecycle-gated this way (it
has no `entitlement_ids`); it has its own resolution gate instead, see
the `TRANSFER` rows below.

| Event | `cancel_reason` / other discriminator | New `status` | `will_renew` | `expires_at` |
|---|---|---|---|---|
| `INITIAL_PURCHASE` | — | `ACTIVE` | `true` | `expiration_at_ms` |
| `RENEWAL` | — | `ACTIVE` | `true` | `expiration_at_ms` (extended) |
| `PRODUCT_CHANGE` | — | `ACTIVE` | `true` | `expiration_at_ms` |
| `UNCANCELLATION` | — | `ACTIVE` (unchanged) | `true` (restored) | unchanged |
| `CANCELLATION` | not `CUSTOMER_SUPPORT` | unchanged (preserves whatever was already projected — `ACTIVE` if nothing existed yet) | `false` | unchanged |
| `CANCELLATION` | `CUSTOMER_SUPPORT` (refund) | `REVOKED` | **preserved** from whatever was already projected (`true` only if no prior row existed) — **never fabricated `false`**, see `REVENUECAT_INTEGRATION_NOTES.md` section 3 | event timestamp (now) |
| `EXPIRATION` | — | `EXPIRED` | `false` | `expiration_at_ms` or event timestamp |
| `BILLING_ISSUE` | — | `GRACE_PERIOD` | `true` (unchanged) | `grace_period_expiration_at_ms` |
| `TRANSFER` (each resolvable `transferred_from` id, excluding the resolved destination) | — | `REVOKED` | `false` | event timestamp (now) |
| `TRANSFER` (the single distinct resolvable `transferred_to` id) | — | `ACTIVE` | `true` | `expiration_at_ms` |
| `TRANSFER` with no resolvable `environment` | — | not applied at all — `RECONCILIATION_REQUIRED` (`UNRESOLVED_ENVIRONMENT`) | — | — |
| `TRANSFER` with zero resolvable `transferred_to` entries | — | not applied at all — `RECONCILIATION_REQUIRED` (`NO_RESOLVABLE_TRANSFER_DESTINATION`) | — | — |
| `TRANSFER` with more than one distinct resolvable `transferred_to` entry | — | not applied at all — `RECONCILIATION_REQUIRED` (`AMBIGUOUS_TRANSFER_DESTINATION`) | — | — |
| a lifecycle event whose `entitlement_ids` doesn't include the configured entitlement (or is `null`) | — | not applied — `NOT_RELEVANT` | — | — |
| any other event type (`VIRTUAL_CURRENCY_TRANSACTION`, `EXPERIMENT_ENROLLMENT`, `NON_RENEWING_PURCHASE`, `SUBSCRIPTION_PAUSED`, `SUBSCRIPTION_EXTENDED`, `REFUND_REVERSED`, `TEMPORARY_ENTITLEMENT_GRANT`, `INVOICE_ISSUANCE`, etc. — see `REVENUECAT_INTEGRATION_NOTES.md` section 6 for the full classification) | — | no projection write; durable receipt only (`PROCESSED`) | — | — |

Every row above (except the gating/`TRANSFER`-resolution outcomes, which never reach the write at all) is applied only if the incoming event's `event_timestamp_ms` is not older than the entitlement row's current `last_provider_event_at` — see `BILLING_ARCHITECTURE.md`'s "Out-of-order protection" section. A stale write is recorded on `revenuecat_webhook_events.processing_status = STALE_IGNORED` and never applied.

## Reconciliation's own transitions

`RevenueCatReconciliationService` (see `BILLING_ARCHITECTURE.md`) writes directly to `ACTIVE` or `EXPIRED` based on the live RevenueCat `active_entitlements` response, with `source_event_id = NULL` (no single webhook event to attribute the correction to) and `last_provider_event_at = now()` — deliberately always new enough to supersede whatever webhook-driven state came before it, since reconciliation exists specifically to correct drift. Correcting to `ACTIVE` **preserves** whatever `will_renew` was already locally known (the active-entitlements response has no renewal-state field to read one from) rather than fabricating `true`; correcting to `EXPIRED` always sets `will_renew=false`.

## `revenuecat_webhook_events.processing_status`

| Status | Meaning |
|---|---|
| `PENDING` | Durably received, not yet processed (or currently claimed by a worker). |
| `PROCESSED` | Applied (or determined to need no projection write — an unhandled event type). |
| `STALE_IGNORED` | Verified and durably stored, but every entitlement row it targeted was already at a newer `last_provider_event_at` — never applied. |
| `FAILED` | A lifecycle event's `app_user_id` did not resolve to a real `users.id` — see `BILLING_ARCHITECTURE.md`'s "Identity" section. Terminal; not retried, since retrying cannot change whether an id string maps to a user. |
| `RECONCILIATION_REQUIRED` | A verified, durably-received event this pass cannot safely auto-apply: a `TRANSFER` with an unresolved `environment`, zero resolvable local `transferred_to` destinations, or more than one distinct resolvable local destination (`AMBIGUOUS_TRANSFER_DESTINATION`). Never retried automatically (it isn't an exception) and never silently dropped — `last_error_code` names the specific reason; an operator/future reconciliation pass must resolve it. |
| `NOT_RELEVANT` | A lifecycle event for a resolved, real user, but whose `entitlement_ids` do not include this deployment's configured entitlement (or is `null`) — an unrelated RevenueCat product. A normal, successful outcome, not an error. |
