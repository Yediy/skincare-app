# Consent / Async Processing Review

Part IV, Phase 21's required review: does the existing consent policy cover
transient server/cloud facial-image processing? Written by inspecting the
actual code in this repository, not assumed, and not padded with invented
legal language.

## What the consent system actually is

`backend/app/db/consent_repository.py` implements a **structured consent
ledger** -- append-only rows keyed by `(consent_type, policy_version)`, with
`REQUIRED_CONSENT_TYPE = "facial_analysis"` and `REQUIRED_POLICY_VERSION =
"1.0"` as the server's own single source of truth for what's currently
required. `/analyze` (and, this pass, the new async submission path) both
correctly refuse to proceed without a valid, current-version grant.

**What this repository does NOT contain: the actual policy text.** There is
no legal/product-authored consent document, privacy notice, or disclosure
copy anywhere in this codebase -- `policy_version` is a bare string
identifier (`"1.0"`), and `purpose` (the other field a caller supplies when
granting consent) is free text the *caller* provides (e.g. this pass's own
tests pass `"facial skin analysis"`), not a canonical, server-defined
description of what actually happens to the data. The real, user-facing
policy language presumably lives in a mobile/web client or a separate legal
document repository -- neither of which exists in, or is reachable from,
this codebase.

**Conclusion: this review cannot verify, from this repository alone,
whether the actual consent language a user sees covers transient
cloud/object-storage processing.** That is not a gap this pass can close by
writing policy text itself -- doing so would be fabricating legal language
this engineering pass has no authority or qualification to write, which
this pass's own instructions explicitly forbid.

## What's genuinely new about async processing, worth flagging to whoever
## does hold that authority

Today (the synchronous `/analyze` path, unchanged by this pass): a
decoded image exists only in the API process's memory for the duration of
one request, is passed directly to `FacialAnalysisPipeline`, and is
discarded -- it is never written to disk, a database, or any object
storage, anywhere, at any point (`PRODUCTION_ARCHITECTURE.md`'s Raw Face
Image Policy, unchanged).

The new async pipeline this pass builds (Part IV/V/VI) introduces a real,
new data-handling fact: a raw face image is, for a short window (target: 1
hour, `app/storage/ephemeral_image_store.py`'s `DEFAULT_RETENTION`),
written to Cloudflare R2 (a third-party cloud storage provider) so a
separate worker process can read it. This is a genuinely different
processing/storage characteristic from "processed in server memory during
one request and never written anywhere" -- even though the object is
private, short-lived, and deleted immediately after processing (see
`RAW_IMAGE_LIFECYCLE.md`), a user-facing consent disclosure that only ever
described "your photo is analyzed on our servers" may not, depending on its
exact wording (which this repository does not contain), fairly disclose
"...and briefly stored, in transit, at a named third-party cloud storage
provider."

## Decision for this pass

**Async image storage is built, but gated off by default, pending a real
legal/product review of the actual consent language.**

`Settings.async_image_storage_enabled: bool = False` (see `app/config.py`)
-- the async submission path (`POST /api/v2/analyses`) refuses to enqueue a
job with `503`/an explicit error when this is `False`, rather than silently
proceeding. This is the "implement the necessary consent-version change
before enabling async image storage in production" requirement, implemented
as an explicit, fail-closed feature flag rather than as a fabricated
version bump: the existing `REQUIRED_POLICY_VERSION` mechanism is exactly
the right tool to force re-consent once the real policy text is updated
(`has_valid_consent()` already checks the exact version, so bumping it
already correctly requires every user to re-consent before analysis can
proceed again) -- but bumping it *now*, without real updated policy text
behind it, would be asserting a compliance claim this pass has no basis to
make.

**Required before this flag is turned on in any real production
environment:**

1. Legal/product review and, if needed, an update to the actual
   user-facing consent/privacy language to explicitly disclose transient
   third-party cloud storage during processing.
2. If that language changes in a way that needs re-consent, bump
   `REQUIRED_POLICY_VERSION` in `backend/app/db/consent_repository.py` to
   the new version, so every existing user is correctly required to
   re-consent before their next analysis (the mechanism for this already
   exists and is already tested -- `tests/auth/test_consent.py`).
3. Only then set `ASYNC_IMAGE_STORAGE_ENABLED=true`.

## What a truthful claim can say today

Not: "raw photos never leave the device" -- server-side analysis (both the
existing synchronous path and this pass's async one) makes that false.

A draft a real policy review could start from, not a substitute for one:

> Raw analysis images are used transiently for processing and are not
> retained as normal account data. During processing, an image may be
> stored briefly (currently: up to one hour) in private cloud object
> storage before being permanently deleted.

This sentence is offered as a starting point for the actual legal/product
review this document defers to -- not as the review itself, and not as a
claim this pass asserts is currently true of the live, user-facing consent
flow (which, per the finding above, this repository cannot verify either
way).
