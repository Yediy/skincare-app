# RevenueCat Integration Notes

Verified against official RevenueCat documentation during this pass
(2026-09-11), not implemented from memory. Sources cited inline. Where
a detail could not be confirmed from an official `revenuecat.com` page
(only from community posts), that is flagged explicitly -- this
document never presents a community answer as an official guarantee.

**Corrected by independent review (same date, before merge):** the
original version of this document and its implementation got two
things wrong that are fixed in migration `9815eb266923` and are
corrected below -- (1) `TRANSFER`'s field group was previously
documented and coded as if it were identical to every other event
(`app_user_id`/`environment` both required); it is not --
`app_user_id` is not part of `TRANSFER` at all. (2) the reconciliation
REST endpoint/response shape (section 5) was previously documented and
implemented against a nonexistent `?expand=active_entitlements` query
parameter; the real, documented resource is a separate paginated
endpoint. Both corrections are reflected in the text below, not just
appended as a note.

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

Common to **every** event, `TRANSFER` included: `api_version`, `type`
(event type), `id` (unique per *logical* event, stable across
redelivery retries), `event_timestamp_ms`, `app_id`.

Identity fields (present on purchase/lifecycle events -- **not on
`TRANSFER`**, see below):

- `app_user_id` -- the App User ID RevenueCat currently associates
  with this event.
- `original_app_user_id` -- the first App User ID ever used for this
  customer.
- `aliases` -- every historical App User ID RevenueCat has merged for
  this customer.
- `environment` -- `SANDBOX` or `PRODUCTION`. Documented as **always**
  present on lifecycle events; only **sometimes** present on
  `TRANSFER` (see below) -- this is a real, event-type-specific
  difference in the field's own guarantee, not an oversight.

`TRANSFER`'s own field group is genuinely different from every other
event type, not a variant of the identity fields above:

- `transferred_from` -- array of source App User IDs. **Always**
  present.
- `transferred_to` -- array of destination App User IDs. **Always**
  present.
- `environment` -- **sometimes** present.
- **`app_user_id` is NOT part of this event type at all.** RevenueCat's
  own sample `TRANSFER` payload has no `app_user_id` field. (The
  original version of this document and its implementation
  incorrectly required `app_user_id` -- and, by extension, always-
  present `environment` -- on every event including `TRANSFER`; fixed
  in migration `9815eb266923`, see section 3 and
  `BILLING_ARCHITECTURE.md`'s "Database privilege boundary" section
  for the schema/processing consequences.)

Subscription/entitlement fields (lifecycle events only):

- `entitlement_ids`, `product_id`, `period_type` (`TRIAL`, `INTRO`,
  `NORMAL`, `PROMOTIONAL`, `PREPAID`), `purchased_at_ms`,
  `expiration_at_ms` (nullable for non-subscription/lifetime),
  `store`, `transaction_id`, `original_transaction_id`.
- `entitlement_ids` is the authoritative list of entitlements this
  specific event's product/subscription is associated with, and may be
  `null` when the product is not mapped to any entitlement. This
  implementation now gates every lifecycle mutation on
  `settings.revenuecat_entitlement_id in (entitlement_ids or [])` --
  see section 3's "entitlement_ids enforcement" and
  `app/domain/revenuecat_entitlement_processor.py`. The original
  version of this pass ignored this field entirely and applied every
  supported lifecycle event unconditionally, which meant an unrelated
  RevenueCat product could grant or revoke this app's premium
  entitlement -- a real bug, fixed in the same migration/processor
  change as the `TRANSFER` correction above.
- `cancel_reason` -- present **only** on `CANCELLATION` events. Values:
  `UNSUBSCRIBE`, `BILLING_ERROR`, `DEVELOPER_INITIATED`,
  `PRICE_INCREASE`, `CUSTOMER_SUPPORT`, `UNKNOWN`.
- `expiration_reason` -- present **only** on `EXPIRATION` events.
  Values: `UNSUBSCRIBE`, `BILLING_ERROR`, `DEVELOPER_INITIATED`,
  `PRICE_INCREASE`, `CUSTOMER_SUPPORT`, `UNKNOWN`,
  `SUBSCRIPTION_PAUSED`.
- `grace_period_expiration_at_ms` -- present (can be `null`) only on
  `BILLING_ISSUE`.

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
  - **Correction (independent review):** a refund revoking the current
    period's access does **not** imply the subscription's renewal
    preference was also turned off -- those are independent facts, both
    surfaced through `CANCELLATION` with different discriminators. The
    original implementation fabricated `will_renew=false` on every
    refund regardless of the subscription's actual renewal setting.
    Fixed: a refund `CANCELLATION` now preserves whatever `will_renew`
    was already locally projected (defaulting `true` only when no prior
    local row exists, which has no access consequence either way since
    the row is `REVOKED`) instead of overwriting it. See
    `app/domain/revenuecat_entitlement_processor.py` and
    `ENTITLEMENT_STATE_MACHINE.md`.
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
  source App User IDs to one or more destination App User IDs.
  **Correction (independent review): there is no `app_user_id` on this
  event.** The destination is `transferred_to` (an array, not a
  singular field) -- the original implementation incorrectly read
  `event.get("app_user_id")` as the destination, which does not exist
  on a real `TRANSFER` payload. `environment` is only sometimes
  present.
  - **Destination resolution policy (this app's own rule, not
    RevenueCat's):** resolve every `transferred_to` entry to an
    internal `users.id`; exactly one *distinct* resolvable local
    destination is required. Zero resolvable destinations, or more
    than one distinct resolvable destination
    (`AMBIGUOUS_TRANSFER_DESTINATION`), both fail closed into
    `RECONCILIATION_REQUIRED` rather than guessing -- this app must
    never grant premium access to multiple unrelated local users
    merely because several aliases happened to appear in
    `transferred_to`.
  - **Missing `environment` policy (this app's own rule):** since
    `environment` is only sometimes present and `user_entitlements.
    environment` stays `NOT NULL`, a `TRANSFER` with no `environment`
    is durably stored (never rejected, never fabricated a
    SANDBOX/PRODUCTION guess) but marked `RECONCILIATION_REQUIRED`
    (`UNRESOLVED_ENVIRONMENT`) rather than applied.
  - Every resolvable source in `transferred_from` is revoked *before*
    the destination is activated (fail-safe ordering: a partial failure
    between the two favors temporary denial over duplicate paid
    access). An unresolvable source alias is simply skipped, never
    fabricated into a user and never a hard failure -- only the
    destination side is required to be certain.
  - Must never leave both source and destination holding the paid
    entitlement afterward.

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

Source: https://www.revenuecat.com/docs/api-v2 (official).

**Corrected by independent review.** The original version of this
document (and `RevenueCatAPIClient`) claimed active entitlements were
available via `GET /v2/projects/{project_id}/customers/{app_user_id}
?expand=active_entitlements`. That endpoint's own documented `expand`
values do not include `active_entitlements` -- only `attributes` -- so
that call never actually returned what this pass assumed. The real,
documented resource is a separate, dedicated endpoint:

- Base URL: `https://api.revenuecat.com/v2`.
- Auth: `Authorization: Bearer <secret API key>`.
- Active entitlements: `GET /v2/projects/{project_id}/customers/{customer_id}/active_entitlements`
  (URL-encode `customer_id`). Returns a paginated `object: "list"`
  resource:
  ```json
  {
    "object": "list",
    "items": [
      {"object": "customer.active_entitlement", "entitlement_id": "...", "expires_at": 1658399423658}
    ],
    "next_page": "...",
    "url": "..."
  }
  ```
  `RevenueCatAPIClient.get_active_entitlements()` follows `next_page`
  up to a bounded `MAX_ACTIVE_ENTITLEMENT_PAGES` (20) so the configured
  entitlement can't be falsely declared absent merely because it's on a
  later page, while still refusing to loop forever on a pathological
  `next_page` chain. The configured entitlement is "active" iff its
  `entitlement_id` appears in `items`; `expires_at` (milliseconds) is
  the only other field this pass relies on. **Does not** depend on
  fabricated fields the previous version assumed -- `active_entitlements.
  items` (wrong nesting), `expiration_at_ms` (wrong key), `gives_access`,
  or `auto_renewal_status` -- none of those exist on this endpoint's
  documented response.
- **Renewal state (`will_renew`) is not in this response at all.**
  Reconciliation never fabricates `will_renew=true` from a response
  that doesn't say so -- correcting a projection to `ACTIVE` preserves
  whatever `will_renew` was already locally known (defaulting `true`
  only when no prior local row exists, matching this app's refund-
  cancellation default for the same "no access consequence either way"
  reason). A future pass wanting authoritative renewal state should
  query a documented subscription resource instead of synthesizing one
  here.
- HTTP failure mapping is deliberate, not generic: `401`->
  `UNAUTHORIZED`, `403`->`FORBIDDEN`, `404`->`CUSTOMER_NOT_FOUND`,
  `429`->`RATE_LIMITED`, `5xx`->`SERVER_ERROR`, a transport-level
  failure (timeout, DNS, connection refused)->`NETWORK_ERROR`. See
  `tests/domain/test_revenuecat_reconciliation_http_contract.py` for
  the httpx.MockTransport-based proof of the exact request URL,
  `Authorization` header, response parsing, pagination, and this
  mapping.
- This pass's reconciliation client only ever performs this one
  read-only, paginated GET. It never calls a write endpoint (e.g.
  granting promotional entitlements) -- see `OPEN_ENGINEERING_ITEMS.md`
  item 4b (admin/support grants) for why that's explicitly out of
  scope.

## 6. Event types this deployment deliberately does not act on

RevenueCat defines several event types this pass's entitlement
processor durably receives (see `app/db/revenuecat_repository.py`'s
`PROCESSED` outcome for "unhandled event type -- no projection write")
but never mutates `user_entitlements` for. Classified explicitly here,
per this pass's own review requirement, rather than left unstated:

| Event type | Classification | Why |
|---|---|---|
| `NON_RENEWING_PURCHASE` | IGNORED SAFELY | A one-time (non-subscription) purchase. Durably received; not projected into the subscription-shaped `user_entitlements` state machine this pass models. Revisit if a non-renewing product is ever mapped to `settings.revenuecat_entitlement_id`. |
| `SUBSCRIPTION_PAUSED` | IGNORED SAFELY | Store-level (Play Store) pause. Access-removal semantics for a pause are close to, but not identical to, `EXPIRATION`'s -- rather than guess, this pass leaves the existing projection as-is (durably received, no mutation) until a real product decision is made. |
| `SUBSCRIPTION_EXTENDED` | IGNORED SAFELY | A support/goodwill extension of the current subscription's expiration. Durably received; not projected -- an operator wanting this reflected locally today has `RevenueCatReconciliationService` available. |
| `REFUND_REVERSED` | IGNORED SAFELY | A previously-refunded transaction had its refund reversed. Durably received; not projected -- reconciliation, not a speculative reversal of this pass's own refund handling, is the safe path back to a correct state. |
| `TEMPORARY_ENTITLEMENT_GRANT` | DEFERRED | Would require this pass's `EntitlementService` boundary to understand a time-boxed grant distinct from a subscription's own `expires_at` -- explicitly out of scope alongside admin/support promotional grants (`OPEN_ENGINEERING_ITEMS.md` item 4b). |
| `INVOICE_ISSUANCE` | IGNORED SAFELY | Billing/invoicing metadata, not an entitlement-affecting event by RevenueCat's own definition. Durably received for audit; never projected. |

None of the above is silently swallowed: every event type this
processor doesn't specifically branch on still lands in
`revenuecat_webhook_events` with `processing_status = PROCESSED` (a
genuine, successful "durable receipt only" outcome, distinct from
`FAILED`/`RECONCILIATION_REQUIRED`), and is visible for audit/investigation.

## 7. What this means for the implementation

| Behavior | Where implemented |
|---|---|
| Raw-body HMAC verification, constant-time compare, timestamp tolerance | `app/security/revenuecat_webhook.py` |
| Authorization header check | `app/security/revenuecat_webhook.py` |
| Dedup by `event.id`, not by signature | `revenuecat_webhook_events.revenuecat_event_id UNIQUE` + `app/db/revenuecat_repository.py` |
| Event-type-aware required-field validation (`TRANSFER` never requires `app_user_id`) | `app/api/v2/webhooks.py` |
| `entitlement_ids` gating -- an unrelated product/entitlement is `NOT_RELEVANT`, never mutates premium | `app/domain/revenuecat_entitlement_processor.py` |
| `CANCELLATION` retains access; `cancel_reason=CUSTOMER_SUPPORT` revokes immediately without fabricating `will_renew=false` | `app/domain/revenuecat_entitlement_processor.py` |
| `EXPIRATION` is the actual access-removal signal | same |
| `BILLING_ISSUE` -> `GRACE_PERIOD`, not revoked | same |
| Out-of-order protection via `event_timestamp_ms` vs. stored `last_provider_event_at` | same |
| `TRANSFER` (`transferred_from`/`transferred_to`, never `app_user_id`) moves, never duplicates, entitlement; ambiguous/unresolvable/no-environment cases fail closed to `RECONCILIATION_REQUIRED` | same |
| Sandbox never grants production access | `user_entitlements` scoped by `environment`; `RevenueCatEntitlementService` filters by the running environment |
| Reconciliation against RevenueCat's real, paginated active-entitlements endpoint | `app/domain/revenuecat_reconciliation_service.py` |
| Ordinary runtime role (`skincare_app`) cannot write billing truth at all -- only the dedicated `skincare_billing` role can | migration `9815eb266923`, see `BILLING_ARCHITECTURE.md`'s "Database privilege boundary" |

See `BILLING_ARCHITECTURE.md` for the end-to-end flow and
`ENTITLEMENT_STATE_MACHINE.md` for the full state-transition table.
