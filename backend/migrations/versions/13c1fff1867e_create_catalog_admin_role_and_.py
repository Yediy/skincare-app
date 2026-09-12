"""create skincare_catalog_admin role and grant catalog write privileges

Revision ID: 13c1fff1867e
Revises: 2de8380d3618
Create Date: 2026-09-12 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '13c1fff1867e'
down_revision: Union[str, Sequence[str], None] = '2de8380d3618'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Dedicated, least-privilege database role for catalog ingestion/
    administration -- `skincare_app` (the ordinary runtime role every
    HTTP request connects as) gets no write grant on any table this
    migration touches, exactly the same "ordinary role cannot
    manufacture [X] truth" architecture the billing pass established
    (migrations 9815eb266923, 4e5cda3a6bb0, 1367b870bdcd) for
    `skincare_billing`.

    `skincare_catalog_admin` is created `NOLOGIN` from the start --
    the billing pass's own hard-won lesson (an earlier version of its
    role migration shipped a hardcoded `LOGIN PASSWORD`, independent
    review caught it, and the fix that finally stuck was NOLOGIN-plus-
    a-separately-provisioned-runtime-login) is applied here from day
    one rather than repeated as a mistake first. The actual connectable
    login (this repository's dev/CI infrastructure names it
    `skincare_catalog_runtime` -- see tests/conftest.py -- or whatever
    a given deployment names its equivalent) is provisioned outside
    this migration, with a secret from that deployment's own secret
    manager, and granted membership in this role (`GRANT
    skincare_catalog_admin TO <runtime login>`) so it inherits exactly
    these privileges and no more. `app/config.py::
    _reject_unsafe_production_config` rejects the known dev/CI marker
    for that runtime login's test-only password
    (`skincare_catalog_dev_only`) in `CATALOG_ADMIN_DATABASE_URL` as
    defense in depth, the same posture as the billing pass's own
    `skincare_billing_dev_only` marker.

    `skincare_catalog_admin` is `NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOBYPASSRLS` -- same posture as `skincare_app`/`skincare_billing`.
    None of the tables it can write have RLS (they're global reference
    data, not user-owned -- same rationale as the pre-existing catalog
    tables and the new staging tables from migration 2de8380d3618), so
    NOBYPASSRLS is belt-and-suspenders here rather than the load-
    bearing restriction it is for `skincare_billing` on
    `user_entitlements` -- kept for consistency and because it costs
    nothing.

    Grants, deliberately no more than this pass's own scope needs
    (Section 13 of the brief): every catalog-ingestion staging/
    provenance/audit table, plus the seven existing production catalog
    tables (`brands` through `formulation_ingredients`) that
    `CatalogPublicationService` actually writes. Deliberately NOT
    granted: `ingredient_rules`/`ingredient_interactions` (rule
    authoring is explicitly out of scope for this first ingestion
    pass -- see CATALOG_INGESTION_ARCHITECTURE.md), and nothing
    whatsoever on `users`/`user_profiles`/`consent_events`/
    `analysis_requests`/`analysis_results`/`analysis_measurements`/
    `analysis_usage`/`user_entitlements`/`revenuecat_webhook_events`/
    `refresh_tokens` -- this role has no legitimate reason to touch any
    of them, and no GRANT statement anywhere in this migration
    mentions them. See
    tests/database/test_catalog_admin_privilege.py for the real,
    restricted-role proof (both "can write catalog" and "cannot write
    anything else" halves) and
    tests/database/test_catalog_jobs_and_billing_isolation.py for the
    specific cross-domain checks this brief calls out by name.

    No `DELETE` grant anywhere -- matching this pass's own explicit
    "do not delete formulation A" reformulation requirement and the
    billing pass's identical no-DELETE precedent on
    `user_entitlements`: a superseded formulation is superseded, never
    erased, and `catalog_import_records.raw_payload` is immutable
    evidence, not a row anything should ever remove.
    """
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'skincare_catalog_admin') THEN
                CREATE ROLE skincare_catalog_admin WITH NOLOGIN
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END
        $$;
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO skincare_catalog_admin")

    for table in (
        "catalog_sources", "catalog_import_batches", "catalog_import_records",
        "catalog_review_items", "catalog_formulation_provenance", "catalog_audit_log",
        "brands", "products", "product_formulations", "product_skus",
        "ingredients", "ingredient_aliases", "formulation_ingredients",
    ):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO skincare_catalog_admin")


def downgrade() -> None:
    """Downgrade schema."""
    for table in (
        "formulation_ingredients", "ingredient_aliases", "ingredients",
        "product_skus", "product_formulations", "products", "brands",
        "catalog_audit_log", "catalog_formulation_provenance", "catalog_review_items",
        "catalog_import_records", "catalog_import_batches", "catalog_sources",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM skincare_catalog_admin")
    op.execute("REVOKE USAGE ON SCHEMA public FROM skincare_catalog_admin")
    op.execute("DROP ROLE IF EXISTS skincare_catalog_admin")
