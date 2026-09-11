"""create revenuecat webhook event log and user entitlement projection

Revision ID: a1c9f3e7b2d4
Revises: 219c52642ed4
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9f3e7b2d4'
down_revision: Union[str, Sequence[str], None] = '219c52642ed4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    RevenueCat billing foundation (see BILLING_ARCHITECTURE.md and
    REVENUECAT_INTEGRATION_NOTES.md for the full design). Two tables:

    revenuecat_webhook_events -- the durable receipt of every verified
    webhook delivery, keyed UNIQUE on RevenueCat's own event `id` (not
    on signature, since a redelivery of the same logical event carries
    a fresh signature/timestamp but the same `id` -- see
    REVENUECAT_INTEGRATION_NOTES.md section 1). This is what makes "10
    concurrent deliveries of one event -> 1 durable row" possible: the
    webhook route's INSERT ... ON CONFLICT (revenuecat_event_id) DO
    NOTHING is the actual concurrency guarantee, not application-level
    locking. Deliberately NOT given row-level security, for the same
    reason `jobs` (migration 2e77bc462867) isn't: this is a provider-
    level event log, not literally "owned" by the `app_user_id` string
    it carries (which hasn't even been validated against a real user
    yet at insert time -- that validation happens during processing).
    No route ever exposes this table's rows to an ordinary authenticated
    user; access control for it is "no such route exists," not RLS.
    Stores the event payload for deterministic replay/investigation,
    but never an Authorization header or HMAC secret/signature (those
    never reach this far -- they're verified and discarded by
    app/security/revenuecat_webhook.py before this INSERT happens).

    user_entitlements -- the local, provider-neutral entitlement
    projection `RevenueCatEntitlementService` reads. User-owned data,
    so it gets the same RLS treatment as analysis_requests/user_profiles
    (migration b034483cb876's pattern): isolation scoped by
    app.current_user_id. UNIQUE(user_id, entitlement_identifier,
    provider, environment) means sandbox and production are tracked as
    genuinely separate rows for the same user/entitlement (Section 15
    of the brief: sandbox must never grant production access) --
    RevenueCatEntitlementService itself additionally filters by the
    running environment, so this is defense in depth, not the only
    guard. source_event_id has a real FK into
    revenuecat_webhook_events(revenuecat_event_id) -- nullable (a
    reconciliation-driven correction has no single webhook event to
    attribute to) but, whenever present, must reference a durably
    received event. That FK is a real technical constraint, not just a
    convention: forging an ACTIVE row via this column requires first
    getting a real row into revenuecat_webhook_events, which itself
    requires passing HMAC + Authorization verification. Combined with
    "no HTTP route ever lets a normal user write their own status" and
    no DELETE grant, this is what "the runtime app role must not be
    able to forge arbitrary premium access through an unrestricted
    write path" means concretely here -- see SECURITY_AND_SAFETY_NOTES.md.

    Status values are deliberately enumerated rather than a boolean:
    ACTIVE, GRACE_PERIOD, EXPIRED, REVOKED. GRACE_PERIOD exists because
    BILLING_ISSUE is real, RevenueCat-documented behavior that is
    neither "still fully active" nor "gone" -- collapsing it into a
    boolean would lose exactly the distinction Section 9 of the brief
    asks this schema to preserve.
    """
    op.execute("""
        CREATE TABLE revenuecat_webhook_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            revenuecat_event_id VARCHAR(255) NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            app_user_id VARCHAR(255) NOT NULL,
            environment VARCHAR(20) NOT NULL,
            event_timestamp TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            processing_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code VARCHAR(100),
            payload_json JSONB NOT NULL,
            CONSTRAINT revenuecat_webhook_events_event_id_unique UNIQUE (revenuecat_event_id),
            CONSTRAINT revenuecat_webhook_events_environment_check
                CHECK (environment IN ('SANDBOX', 'PRODUCTION')),
            CONSTRAINT revenuecat_webhook_events_processing_status_check
                CHECK (processing_status IN ('PENDING', 'PROCESSED', 'STALE_IGNORED', 'FAILED'))
        )
    """)
    op.execute("CREATE INDEX idx_revenuecat_webhook_events_app_user_id ON revenuecat_webhook_events(app_user_id)")
    op.execute("CREATE INDEX idx_revenuecat_webhook_events_status ON revenuecat_webhook_events(processing_status)")
    op.execute("GRANT SELECT, INSERT, UPDATE ON revenuecat_webhook_events TO skincare_app")

    op.execute("""
        CREATE TABLE user_entitlements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            entitlement_identifier VARCHAR(100) NOT NULL,
            provider VARCHAR(20) NOT NULL DEFAULT 'revenuecat',
            status VARCHAR(20) NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ,
            will_renew BOOLEAN NOT NULL DEFAULT false,
            environment VARCHAR(20) NOT NULL,
            source_event_id VARCHAR(255) REFERENCES revenuecat_webhook_events(revenuecat_event_id),
            provider_customer_id VARCHAR(255) NOT NULL,
            last_provider_event_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT user_entitlements_unique_scope
                UNIQUE (user_id, entitlement_identifier, provider, environment),
            CONSTRAINT user_entitlements_status_check
                CHECK (status IN ('ACTIVE', 'GRACE_PERIOD', 'EXPIRED', 'REVOKED')),
            CONSTRAINT user_entitlements_environment_check
                CHECK (environment IN ('SANDBOX', 'PRODUCTION'))
        )
    """)
    op.execute("CREATE INDEX idx_user_entitlements_user_id ON user_entitlements(user_id)")
    op.execute("CREATE INDEX idx_user_entitlements_provider_customer_id ON user_entitlements(provider_customer_id)")
    # No DELETE grant: an entitlement row is superseded by a new status
    # transition, never erased -- the row itself is part of the audit
    # trail this pass's brief asks for.
    op.execute("GRANT SELECT, INSERT, UPDATE ON user_entitlements TO skincare_app")

    op.execute("ALTER TABLE user_entitlements ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY user_entitlements_isolation ON user_entitlements
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS user_entitlements_isolation ON user_entitlements")
    op.execute("ALTER TABLE user_entitlements DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON user_entitlements FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS user_entitlements")

    op.execute("REVOKE ALL ON revenuecat_webhook_events FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS revenuecat_webhook_events")
