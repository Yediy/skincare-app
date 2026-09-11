# RevenueCat Integration Notes

Verified against official RevenueCat documentation during this pass
(2026-09-11), not implemented from memory. Sources cited inline. Where
a detail could not be confirmed from an official `revenuecat.com` page
(only from community posts), that is flagged explicitly -- this
document never presents a community answer as an official guarantee.

A third-party gist ("RevenueCatClaw") surfaced repeatedly in search
results for RevenueCat webhook/refund topics. It is **not**
`revenuecat.com` and is **not used anywhere in this document or in the
implementation** -- flagged here in case it resurfaces in a future
pass's research, since it reads as though it wants to be treated as an
authoritative reference.

## 1. Webhook delivery contract

Source: https://www.revenuecat.com/docs/integrations/webhooks

- RevenueCat POSTs a JSON body to the configured webhook URL for every
  event.
- **Authorization header**: an optional, dashboard-configured static
  value RevenueCat echoes back in the `Authorization` header of every
  webhook request. Plain shared-secret comparison, not a signature.
- **HMAC signature**: `X-RevenueCat-Webhook-Signature: t=<unix_ts>,v1=<hmac_sha256_hex>`.
  Compute HMAC-SHA256 over the literal string `"{t}.{raw_request_body}"`
  (the timestamp, a literal `.`, then the *raw* request body bytes --
  never re-serialized JSON) using the integration's signing secret,
  hex-encode, compare with `hmac.compare_digest` (constant-time).
  Optionally reject if `|now - t|` exceeds a configured tolerance
  (RevenueCat's own docs suggest ~5 minutes, to guard against replay).
- **Response contract**: return **HTTP 200 within 60 seconds**. Any
  other status (or a timeout) is treated as a failed delivery.
- **Retry policy**: failed deliveries are retried up to 5 times with
  increasing delay (~5, 10, 20, 40, 80 minutes). The redelivered
  payload carries the **same event `id`** as the original attempt, but
  the HMAC signature is recomputed with a fresh timestamp each retry
  (the original signature is not reused) -- so idempotency must key off
  the payload's `id` field, not off "have I seen this exact signature
  before."

**Implication for this pass**: verify the signature per-request (each
delivery attempt gets its own valid signature), then deduplicate by
`event.id`, not by signature.

## 2. Event envelope and identity fields

Source: https://www.revenuecat.com/docs/integrations/webhooks/event-types-and-fields

Common to every event:

- `api_version`, `type` (event type), `id` (unique per *logical* event,
  stable across redelivery retries), `event_timestamp_ms`, `app_id`.

Identity fields (present on purchase/lifecycle events):

- `app_user_id` -- the App User ID RevenueCat currently associates
  with this event.
- `original_app_user_id` -- the first App User ID ever used for this
  customer.
- `aliases` -- every historical App User ID RevenueCat has merged for
  this customer.
- `environment` -- `SANDBOX` or `PRODUCTION`.

Subscription/entitlement fields:

- `entitlement_ids`, `product_id`, `period_type` (`TRIAL`, `INTRO`,
  `NORMAL`, `PROMOTIONAL`, `PREPAID`), `purchased_at_ms`,
  `expiration_at_ms` (nullable for non-subscription/lifetime),
  `store`, `transaction_id`, `original_transaction_id`.
- `cancel_reason` -- present **only** on `CANCELLATION` events. Values:
  `UNSUBSCRIBE`, `BILLING_ERROR`, `DEVELOPER_INITIATED`,
  `PRICE_INCREASE`, `CUSTOMER_SUPPORT`, `UNKNOWN`.
- `expiration_reason` -- present **only** on `EXPIRATION` events.
  Values: `UNSUBSCRIBE`, `BILLING_ERROR`, `DEVELOPER_INITIATED`,
  `PRICE_INCREASE`, `CUSTOMER_SUPPORT`, `UNKNOWN`,
  `SUBSCRIPTION_PAUSED`.
- `grace_period_expiration_at_ms` -- present (can be `null`) only on
  `BILLING_ISSUE`.
- `transferred_from` / `transferred_to` -- arrays of App User IDs,
  present only on `TRANSFER`. The webhook's own `app_user_id` is the
  transfer's destination.

## 3. Event semantics (this is the part most easily gotten wrong)

Source: official docs above + RevenueCat community answers cross-
checked against them (flagged where community-sourced).

- **`INITIAL_PURCHASE`** -- new subscription/purchase. Activate the
  entitlement.
- **`RENEWAL`** -- existing subscription renewed, or a lapsed
  subscriber resubscribed. Extend/reactivate.
- **`PRODUCT_CHANGE`** -- subscriber changed product (e.g. plan tier).
  Not supported on every store.
- **`CANCELLATION`** -- **does not mean access ends now.** Per the
  official docs, `EXPIRATION` -- not `CANCELLATION` -- is the event
  whose associated access "should be removed." `CANCELLATION` fires
  "whenever it becomes clear the subscription will lapse unless the
  customer takes action" -- i.e. auto-renew was turned off, or (see
  next bullet) a refund happened. This event typically fires **well
  before** the paid period actually ends.
  - **Refund detection is via `cancel_reason`, not the event type.**
    `cancel_reason=CUSTOMER_SUPPORT` is the refund marker (a support-
    initiated or user-initiated refund of the current period). A
    subscription's auto-renew setting can still be *on* even when a
    refund CANCELLATION fires -- refund and auto-renew-off are
    independent facts, both surfaced through the same event type with
    different `cancel_reason` values. (Cross-checked against RevenueCat
    community answers; the official event-types page confirms the
    `cancel_reason` enum and its "sometimes included" status but does
    not itself spell out the refund-vs-voluntary distinction in as many
    words -- treated here as the correct read, not a guess, since it
    matches the field's own documented purpose.)
  - This pass's rule: `cancel_reason == CUSTOMER_SUPPORT` -> treat as
    an immediate-effect refund (revoke now). Every other `cancel_reason`
    -> normal cancellation (retain access through the existing
    `expires_at`, just flip `will_renew` off).
- **`UNCANCELLATION`** -- a non-expired, previously-canceled
  subscription had auto-renew re-enabled. Restore `will_renew=true`;
  status/access is unaffected (it was never revoked by the
  cancellation in the first place).
- **`EXPIRATION`** -- the subscription's paid period is actually over.
  This is the real "remove access" signal.
- **`BILLING_ISSUE`** -- a renewal charge attempt failed.
  `grace_period_expiration_at_ms` indicates RevenueCat/the store may
  still grant access through a grace window. Per the docs: "this
  doesn't mean the subscription has expired." Does not by itself
  change access; enters `GRACE_PERIOD` in this app's own projection
  (see `ENTITLEMENT_STATE_MACHINE.md`).
- **`TRANSFER`** -- transactions/entitlements moved from one or more
  source App User IDs to a destination. The event's own `app_user_id`
  is the destination; `transferred_from` lists the source(s). Must
  never leave both source and destination holding the paid entitlement
  afterward.

## 4. Aliases

An App User ID is not necessarily permanent identity-for-identity --
RevenueCat can merge multiple App User IDs (e.g. an anonymous ID and a
later-identified one) into one customer, exposed via `aliases`.

**This app's own identity policy (Section 2 of the implementation
brief) sidesteps most of this complexity deliberately**: the
application's own immutable user UUID is used as the canonical
RevenueCat App User ID from the first SDK call (no anonymous
pre-login purchase flow exists, since no mobile app exists yet
either). So in steady state, `app_user_id` on every event should
already equal a real internal `users.id`. `aliases` is still recorded
(inside `payload_json`, and via the FK/audit trail on
`revenuecat_webhook_events`) for forensic value, but this pass does
**not** auto-trust an alias string as authorization to write another
user's entitlement row -- only `app_user_id` (and, for `TRANSFER`,
`transferred_from`/`transferred_to`) drive projection writes, and only
after each resolves to a `users.id` that actually exists.

## 5. REST API (used only by reconciliation, never on the request path)

Source: https://www.revenuecat.com/docs/api-v2 (official), cross-
checked with RevenueCat community threads for the exact customer path
(flagged below).

- Base URL: `https://api.revenuecat.com/v2`.
- Auth: `Authorization: Bearer <secret API key>`.
- Customer lookup: `GET /v2/projects/{project_id}/customers/{app_user_id}`
  (URL-encode `app_user_id`), with `active_entitlements` available via
  `?expand=active_entitlements` (per RevenueCat community confirmation
  -- the exact query-param expansion syntax was not reproducible
  verbatim from the official page in this pass's fetch, so
  `RevenueCatAPIClient` treats the response defensively: an
  unexpected/missing field is a reconciliation *mismatch* to report,
  never a silent assumption).
- This pass's reconciliation client only ever performs this one
  read-only GET. It never calls a write endpoint (e.g. granting
  promotional entitlements) -- see `OPEN_ENGINEERING_ITEMS.md` item 16
  (admin/support grants) for why that's explicitly out of scope.

## 6. What this means for the implementation

| Behavior | Where implemented |
|---|---|
| Raw-body HMAC verification, constant-time compare, timestamp tolerance | `app/security/revenuecat_webhook.py` |
| Authorization header check | `app/security/revenuecat_webhook.py` |
| Dedup by `event.id`, not by signature | `revenuecat_webhook_events.revenuecat_event_id UNIQUE` + `app/db/revenuecat_repository.py` |
| `CANCELLATION` retains access; `cancel_reason=CUSTOMER_SUPPORT` revokes immediately | `app/domain/revenuecat_entitlement_processor.py` |
| `EXPIRATION` is the actual access-removal signal | same |
| `BILLING_ISSUE` -> `GRACE_PERIOD`, not revoked | same |
| Out-of-order protection via `event_timestamp_ms` vs. stored `last_provider_event_at` | same |
| `TRANSFER` moves, never duplicates, entitlement | same |
| Sandbox never grants production access | `user_entitlements` scoped by `environment`; `RevenueCatEntitlementService` filters by the running environment |
| Reconciliation against RevenueCat's read-only customer API | `app/domain/revenuecat_reconciliation_service.py` |

See `BILLING_ARCHITECTURE.md` for the end-to-end flow and
`ENTITLEMENT_STATE_MACHINE.md` for the full state-transition table.
