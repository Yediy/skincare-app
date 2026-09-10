"""create product catalog core tables

Revision ID: d70e5fc90775
Revises: 2e77bc462867
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd70e5fc90775'
down_revision: Union[str, Sequence[str], None] = '2e77bc462867'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Normalized product catalog whose safety boundary sits at the
    FORMULATION level, not the product or brand: brands -> products ->
    product_formulations -> product_skus, with ingredients resolved
    through ingredient_aliases so free-text label variance ("Vitamin
    C" vs "L-Ascorbic Acid" vs "ascorbic acid") converges on one
    canonical ingredient row rather than needing substring matching at
    query time.

    This is global reference data, not user-owned data: no RLS here
    (there is no per-row "owner" to scope by), and skincare_app gets
    SELECT-only grants -- no INSERT/UPDATE/DELETE at all. Nothing in
    this pass builds a catalog-administration route, so there is no
    legitimate reason for the runtime API role to be able to write
    safety-relevant catalog data; population happens via migrations
    (this file's own seed-free schema) and, for tests, the superuser
    test-fixture path (conftest.py's TEST_DATABASE_URL), matching the
    existing test-owner-seeds/runtime-role-reads split used everywhere
    else in this repository.

    formulation_ingredients has no surrogate id (matching the
    minimum-fields spec this table was built against exactly): its
    primary key is (formulation_id, ingredient_id), since an
    ingredient does not repeat within one formulation.
    """
    op.execute("""
        CREATE TABLE brands (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name VARCHAR(255) NOT NULL,
            normalized_name VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT brands_normalized_name_unique UNIQUE (normalized_name)
        )
    """)

    op.execute("""
        CREATE TABLE products (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            brand_id UUID NOT NULL REFERENCES brands(id),
            name VARCHAR(255) NOT NULL,
            normalized_name VARCHAR(255) NOT NULL,
            category VARCHAR(100) NOT NULL,
            description TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT products_status_check CHECK (status IN ('active', 'discontinued', 'draft')),
            CONSTRAINT products_brand_normalized_name_unique UNIQUE (brand_id, normalized_name)
        )
    """)
    op.execute("CREATE INDEX idx_products_brand_id ON products(brand_id)")
    op.execute("CREATE INDEX idx_products_category ON products(category)")

    # A product may have multiple formulations (by market/region,
    # reformulation date, revision). Safety truth belongs here, not on
    # `products` -- see PRODUCT_CATALOG_ARCHITECTURE.md.
    op.execute("""
        CREATE TABLE product_formulations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            product_id UUID NOT NULL REFERENCES products(id),
            version VARCHAR(50) NOT NULL,
            market_or_region VARCHAR(50) NOT NULL DEFAULT 'global',
            effective_from DATE,
            effective_to DATE,
            is_current BOOLEAN NOT NULL DEFAULT true,
            source_type VARCHAR(50) NOT NULL,
            source_reference TEXT,
            verified_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT product_formulations_source_type_check
                CHECK (source_type IN ('manufacturer_label', 'manufacturer_disclosure', 'regulatory_filing', 'third_party_verified', 'user_submitted_unverified')),
            CONSTRAINT product_formulations_product_version_market_unique
                UNIQUE (product_id, version, market_or_region)
        )
    """)
    op.execute("CREATE INDEX idx_product_formulations_product_id ON product_formulations(product_id)")
    # At most one *current* formulation per product+market -- this is
    # the actual invariant "which formulation applies right now" leans
    # on; a partial unique index is the correct way to enforce that in
    # Postgres, since a plain UNIQUE constraint can't be conditional.
    op.execute("""
        CREATE UNIQUE INDEX idx_product_formulations_one_current_per_market
            ON product_formulations(product_id, market_or_region)
            WHERE is_current = true
    """)

    op.execute("""
        CREATE TABLE product_skus (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            product_id UUID NOT NULL REFERENCES products(id),
            formulation_id UUID NOT NULL REFERENCES product_formulations(id),
            sku VARCHAR(100) NOT NULL,
            upc_or_ean VARCHAR(50),
            size_value NUMERIC(10, 2),
            size_unit VARCHAR(20),
            market_or_region VARCHAR(50) NOT NULL DEFAULT 'global',
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT product_skus_product_sku_unique UNIQUE (product_id, sku)
        )
    """)
    op.execute("CREATE INDEX idx_product_skus_product_id ON product_skus(product_id)")
    op.execute("CREATE INDEX idx_product_skus_formulation_id ON product_skus(formulation_id)")

    # Canonical ingredient entities. `normalized_name` is the
    # lowercased/trimmed form every lookup actually queries by --
    # substring/LIKE matching against free text is exactly what this
    # table (plus ingredient_aliases below) exists to make unnecessary.
    op.execute("""
        CREATE TABLE ingredients (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            canonical_name VARCHAR(255) NOT NULL,
            normalized_name VARCHAR(255) NOT NULL,
            inci_name VARCHAR(255),
            ingredient_type VARCHAR(50),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ingredients_normalized_name_unique UNIQUE (normalized_name)
        )
    """)

    # Every alias resolves to exactly one canonical ingredient --
    # normalized_alias is globally unique so resolution is a single
    # deterministic lookup, never an ambiguous one.
    op.execute("""
        CREATE TABLE ingredient_aliases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ingredient_id UUID NOT NULL REFERENCES ingredients(id),
            alias VARCHAR(255) NOT NULL,
            normalized_alias VARCHAR(255) NOT NULL,
            alias_type VARCHAR(50) NOT NULL DEFAULT 'synonym',
            CONSTRAINT ingredient_aliases_normalized_alias_unique UNIQUE (normalized_alias),
            CONSTRAINT ingredient_aliases_alias_type_check
                CHECK (alias_type IN ('synonym', 'inci', 'trade_name', 'common_name', 'abbreviation'))
        )
    """)
    op.execute("CREATE INDEX idx_ingredient_aliases_ingredient_id ON ingredient_aliases(ingredient_id)")

    # No surrogate id (see docstring). NULL declared_concentration is a
    # real, meaningful value ("undisclosed"), never fabricated.
    op.execute("""
        CREATE TABLE formulation_ingredients (
            formulation_id UUID NOT NULL REFERENCES product_formulations(id),
            ingredient_id UUID NOT NULL REFERENCES ingredients(id),
            position INTEGER NOT NULL,
            declared_concentration NUMERIC(6, 3),
            concentration_unit VARCHAR(20),
            notes TEXT,
            PRIMARY KEY (formulation_id, ingredient_id),
            CONSTRAINT formulation_ingredients_position_unique UNIQUE (formulation_id, position)
        )
    """)
    op.execute("CREATE INDEX idx_formulation_ingredients_ingredient_id ON formulation_ingredients(ingredient_id)")

    for table in (
        "brands", "products", "product_formulations", "product_skus",
        "ingredients", "ingredient_aliases", "formulation_ingredients",
    ):
        op.execute(f"GRANT SELECT ON {table} TO skincare_app")


def downgrade() -> None:
    """Downgrade schema."""
    for table in (
        "formulation_ingredients", "ingredient_aliases", "ingredients",
        "product_skus", "product_formulations", "products", "brands",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM skincare_app")

    op.execute("DROP TABLE IF EXISTS formulation_ingredients")
    op.execute("DROP TABLE IF EXISTS ingredient_aliases")
    op.execute("DROP TABLE IF EXISTS ingredients")
    op.execute("DROP TABLE IF EXISTS product_skus")
    op.execute("DROP TABLE IF EXISTS product_formulations")
    op.execute("DROP TABLE IF EXISTS products")
    op.execute("DROP TABLE IF EXISTS brands")
