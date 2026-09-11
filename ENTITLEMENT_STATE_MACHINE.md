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

| Event | `cancel_reason` / other discriminator | New `status` | `will_renew` | `expires_at` |
|---|---|---|---|---|
| `INITIAL_PURCHASE` | — | `ACTIVE` | `true` | `expiration_at_ms` |
| `RENEWAL` | — | `ACTIVE` | `true` | `expiration_at_ms` (extended) |
| `PRODUCT_CHANGE` | — | `ACTIVE` | `true` | `expiration_at_ms` |
| `UNCANCELLATION` | — | `ACTIVE` (unchanged) | `true` (restored) | unchanged |
| `CANCELLATION` | not `CUSTOMER_SUPPORT` | unchanged (preserves whatever was already projected — `ACTIVE` if nothing existed yet) | `false` | unchanged |
| `CANCELLATION` | `CUSTOMER_SUPPORT` (refund) | `REVOKED` | `false` | event timestamp (now) |
| `EXPIRATION` | — | `EXPIRED` | `false` | `expiration_at_ms` or event timestamp |
| `BILLING_ISSUE` | — | `GRACE_PERIOD` | `true` (unchanged) | `grace_period_expiration_at_ms` |
| `TRANSFER` (source, each resolvable `transferred_from` id) | — | `REVOKED` | `false` | event timestamp (now) |
| `TRANSFER` (destination, the event's own `app_user_id`) | — | `ACTIVE` | `true` | `expiration_at_ms` |
| any other event type (`VIRTUAL_CURRENCY_TRANSACTION`, `EXPERIMENT_ENROLLMENT`, etc.) | — | no projection write; durable receipt only | — | — |

Every row above is applied only if the incoming event's `event_timestamp_ms` is not older than the entitlement row's current `last_provider_event_at` — see `BILLING_ARCHITECTURE.md`'s "Out-of-order protection" section. A stale write is recorded on `revenuecat_webhook_events.processing_status = STALE_IGNORED` and never applied.

## Reconciliation's own transitions

`RevenueCatReconciliationService` (see `BILLING_ARCHITECTURE.md`) writes directly to `ACTIVE` or `EXPIRED` based on the live RevenueCat customer response, with `source_event_id = NULL` (no single webhook event to attribute the correction to) and `last_provider_event_at = now()` — deliberately always new enough to supersede whatever webhook-driven state came before it, since reconciliation exists specifically to correct drift.

## `revenuecat_webhook_events.processing_status`

| Status | Meaning |
|---|---|
| `PENDING` | Durably received, not yet processed (or currently claimed by a worker). |
| `PROCESSED` | Applied (or determined to need no projection write — an unhandled event type). |
| `STALE_IGNORED` | Verified and durably stored, but every entitlement row it targeted was already at a newer `last_provider_event_at` — never applied. |
| `FAILED` | `app_user_id` (or, for `TRANSFER`, the destination id) did not resolve to a real `users.id` — see `BILLING_ARCHITECTURE.md`'s "Identity" section. Terminal; not retried, since retrying cannot change whether an id string maps to a user. |
