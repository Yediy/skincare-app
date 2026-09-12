"""correct TRANSFER field contract and separate billing-writer privilege

Revision ID: 9815eb266923
Revises: a1c9f3e7b2d4
Create Date: 2026-09-11 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9815eb266923'
down_revision: Union[str, Sequence[str], None] = 'a1c9f3e7b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Two independent corrections to migration a1c9f3e7b2d4, both driven
    by an independent review against RevenueCat's actual documented
    contract:

    1. REAL TRANSFER FIELD CONTRACT. RevenueCat's TRANSFER event does
    not carry `app_user_id` at all -- it carries `transferred_from`/
    `transferred_to` (always) and `environment` (sometimes). The
    original migration made `app_user_id` and `environment` both
    NOT NULL on `revenuecat_webhook_events`, which meant a real,
    legitimate TRANSFER delivery would either be rejected outright or
    force a fabricated value into a column RevenueCat never populated.
    Both columns become nullable here. A TRANSFER whose `environment`
    RevenueCat omitted is still durably received (this column going
    NULL, never a fabricated SANDBOX/PRODUCTION guess) but cannot be
    safely projected into `user_entitlements` (whose own `environment`
    column stays NOT NULL -- this migration does not relax that one)
    until a human/reconciliation resolves which environment it belongs
    to. `revenuecat_webhook_events.processing_status` gains two new
    terminal values to make that, and an analogous unrelated-product
    case, first-class outcomes rather than overloading `FAILED`
    (which this codebase's worker treats as "not retryable, something
    is wrong") or `STALE_IGNORED` (which means something different --
    a genuinely newer event already superseded this one):

       - RECONCILIATION_REQUIRED: a verified, durably-received event
         this pass cannot safely auto-apply (TRANSFER with no
         resolvable environment, zero resolvable local destination
         users, or more than one distinct resolvable local destination
         user -- AMBIGUOUS_TRANSFER_DESTINATION). Never silently
         dropped; an operator/future reconciliation pass must resolve
         it. See app/domain/revenuecat_entitlement_processor.py.
       - NOT_RELEVANT: a lifecycle event for a real, resolved user, but
         whose `entitlement_ids` do not include this deployment's
         configured `settings.revenuecat_entitlement_id` -- e.g. a
         product unrelated to this app's premium entitlement. This is
         a normal, successful outcome (not an error, not retried), see
         Section on "entitlement_ids enforcement" in
         REVENUECAT_INTEGRATION_NOTES.md.

    2. DATABASE PRIVILEGE BOUNDARY. The original migration granted the
    ordinary runtime role (`skincare_app` -- the same role every
    HTTP-request-serving connection in this application authenticates
    as) direct INSERT/UPDATE on both `revenuecat_webhook_events` and
    `user_entitlements`. RLS on `user_entitlements` only ever stopped a
    *cross-user* write; it does nothing to stop the ordinary runtime
    role from writing an ACTIVE row for whatever user_id the *current*
    request's own session context happens to be set to, or inserting
    a fabricated `revenuecat_webhook_events` row outright (that table
    has no RLS at all -- it's a provider-level log, not row-owned).
    `source_event_id` is nullable specifically so reconciliation can
    write corrections with no webhook event to attribute to, which
    means the FK from `user_entitlements.source_event_id` is not an
    anti-forgery boundary either: an ordinary request can simply omit
    it.

    This migration takes both write grants away from `skincare_app`
    entirely (it keeps SELECT on `user_entitlements` only -- that is
    what `RevenueCatEntitlementService`'s read path needs, and all it
    needs) and introduces a new, dedicated `skincare_billing` role that
    holds them instead, plus the `jobs` grants webhook ingestion and
    the RevenueCat worker need to durably enqueue/claim/acknowledge
    jobs in the same transaction as their table writes. `skincare_app`
    can no longer manufacture billing truth under any circumstance --
    not merely "cannot manufacture *another user's*" truth, the
    stronger claim this pass's brief requires. See
    BILLING_ARCHITECTURE.md's "Database privilege boundary" section
    and tests/database/test_revenuecat_billing_privilege.py.

    `skincare_billing` is NOSUPERUSER / NOCREATEDB / NOCREATEROLE /
    NOBYPASSRLS, same posture as `skincare_app` -- it is still subject
    to `user_entitlements`' own RLS policy (the app code sets
    `app.current_user_id` to the row's own user_id before writing,
    exactly as `skincare_app` did), least privilege rather than a
    blanket bypass.

    `skincare_billing` also needs plain SELECT on `users`:
    `_resolve_user()` (app/domain/revenuecat_entitlement_processor.py)
    confirms an incoming app_user_id/transferred_from/transferred_to
    string is a real users.id before ever writing anything -- the same
    set-app.current_user_id-then-select-by-that-same-id RLS path
    `skincare_app` already used pre-existing there is no privileged
    read path for this table, and there doesn't need to be. No INSERT/
    UPDATE/DELETE grant on `users` -- the billing role never creates or
    modifies a user, only confirms one exists.
    """
    op.execute("ALTER TABLE revenuecat_webhook_events ALTER COLUMN app_user_id DROP NOT NULL")
    op.execute("ALTER TABLE revenuecat_webhook_events ALTER COLUMN environment DROP NOT NULL")

    # RECONCILIATION_REQUIRED (24 chars) exceeds the original
    # VARCHAR(20) sizing of processing_status.
    op.execute("ALTER TABLE revenuecat_webhook_events ALTER COLUMN processing_status TYPE VARCHAR(30)")
    op.execute(
        "ALTER TABLE revenuecat_webhook_events "
        "DROP CONSTRAINT revenuecat_webhook_events_processing_status_check"
    )
    op.execute("""
        ALTER TABLE revenuecat_webhook_events
        ADD CONSTRAINT revenuecat_webhook_events_processing_status_check
            CHECK (processing_status IN (
                'PENDING', 'PROCESSED', 'STALE_IGNORED', 'FAILED',
                'RECONCILIATION_REQUIRED', 'NOT_RELEVANT'
            ))
    """)

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'skincare_billing') THEN
                CREATE ROLE skincare_billing WITH LOGIN PASSWORD 'skincare_billing_dev_only'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END
        $$;
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO skincare_billing")

    # Take the write grants away from the ordinary runtime role...
    op.execute("REVOKE ALL ON revenuecat_webhook_events FROM skincare_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON user_entitlements FROM skincare_app")

    # ...and give them to the dedicated billing role instead. `jobs` is
    # needed too: the webhook route's durable-insert + enqueue happens
    # in one transaction (see app/api/v2/webhooks.py), so whichever
    # role runs that transaction needs privilege on both tables; the
    # worker needs the same for claim/acknowledge/fail.
    op.execute("GRANT SELECT, INSERT, UPDATE ON revenuecat_webhook_events TO skincare_billing")
    op.execute("GRANT SELECT, INSERT, UPDATE ON user_entitlements TO skincare_billing")
    op.execute("GRANT SELECT, INSERT, UPDATE ON jobs TO skincare_billing")
    # Identity resolution only -- see the docstring above.
    op.execute("GRANT SELECT ON users TO skincare_billing")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE ALL ON users FROM skincare_billing")
    op.execute("REVOKE ALL ON jobs FROM skincare_billing")
    op.execute("REVOKE ALL ON user_entitlements FROM skincare_billing")
    op.execute("REVOKE ALL ON revenuecat_webhook_events FROM skincare_billing")
    op.execute("REVOKE USAGE ON SCHEMA public FROM skincare_billing")
    op.execute("DROP ROLE IF EXISTS skincare_billing")

    op.execute("GRANT SELECT, INSERT, UPDATE ON user_entitlements TO skincare_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON revenuecat_webhook_events TO skincare_app")

    op.execute(
        "ALTER TABLE revenuecat_webhook_events "
        "DROP CONSTRAINT revenuecat_webhook_events_processing_status_check"
    )
    op.execute("""
        ALTER TABLE revenuecat_webhook_events
        ADD CONSTRAINT revenuecat_webhook_events_processing_status_check
            CHECK (processing_status IN ('PENDING', 'PROCESSED', 'STALE_IGNORED', 'FAILED'))
    """)

    op.execute("ALTER TABLE revenuecat_webhook_events ALTER COLUMN processing_status TYPE VARCHAR(20)")
    op.execute("ALTER TABLE revenuecat_webhook_events ALTER COLUMN environment SET NOT NULL")
    op.execute("ALTER TABLE revenuecat_webhook_events ALTER COLUMN app_user_id SET NOT NULL")
