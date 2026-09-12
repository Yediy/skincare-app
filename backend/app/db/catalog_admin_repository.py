"""Raw SQL for the catalog-ingestion/administration domain: the
staging tables (migration 2de8380d3618), provenance, audit log, and
the production-catalog WRITE paths (brands/products/product_
formulations/product_skus/ingredients/ingredient_aliases) that only
the dedicated `skincare_catalog_admin` role (migration 13c1fff1867e)
can use. Every function here is called with the catalog-admin pool/
connection -- never `skincare_app`'s.

Production-catalog READ paths are deliberately NOT duplicated here --
app/db/catalog_repository.py already provides
resolve_ingredient/normalize_name/get_product_by_id etc., and those
same read-only queries work identically over any pool with SELECT
grants (which skincare_catalog_admin has, same as skincare_app), so
catalog ingestion code imports and reuses them directly rather than
maintaining a second copy.
"""
import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

import asyncpg

from app.db.catalog_repository import normalize_name

# ---------------------------------------------------------------------------
# catalog_sources
# ---------------------------------------------------------------------------


async def get_source_by_id(pool: asyncpg.Pool, source_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow("SELECT * FROM catalog_sources WHERE id = $1", source_id)
    return dict(row) if row is not None else None


async def get_source_by_name(pool: asyncpg.Pool, name: str) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow(
        "SELECT * FROM catalog_sources WHERE normalized_name = $1", normalize_name(name)
    )
    return dict(row) if row is not None else None


async def create_source(
    pool: asyncpg.Pool, *, name: str, source_type: str, description: Optional[str] = None,
) -> Dict[str, Any]:
    """No credentials field exists on this table at all -- see migration
    2de8380d3618's own docstring; acquisition/credential management is
    a deliberately separate, out-of-scope concern this pass."""
    row = await pool.fetchrow(
        """
        INSERT INTO catalog_sources (name, normalized_name, source_type, description)
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        name, normalize_name(name), source_type, description,
    )
    return dict(row)


# ---------------------------------------------------------------------------
# catalog_import_batches
# ---------------------------------------------------------------------------


async def get_or_create_batch(
    conn: asyncpg.Connection, *, source_id: UUID, content_sha256: str, source_reference: Optional[str],
    records_total: int,
) -> tuple[Dict[str, Any], bool]:
    """Whole-file idempotency: importing byte-identical content from
    the same source twice resolves to the SAME batch row (is_new=False
    the second time), never a duplicate."""
    row = await conn.fetchrow(
        """
        INSERT INTO catalog_import_batches
            (source_id, source_reference, content_sha256, status, records_total, started_at)
        VALUES ($1, $2, $3, 'RECEIVED', $4, now())
        ON CONFLICT (source_id, content_sha256) DO NOTHING
        RETURNING *
        """,
        source_id, source_reference, content_sha256, records_total,
    )
    if row is not None:
        return dict(row), True
    existing = await conn.fetchrow(
        "SELECT * FROM catalog_import_batches WHERE source_id = $1 AND content_sha256 = $2",
        source_id, content_sha256,
    )
    return dict(existing), False


async def get_batch(pool: asyncpg.Pool, batch_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow("SELECT * FROM catalog_import_batches WHERE id = $1", batch_id)
    return dict(row) if row is not None else None


async def update_batch(
    conn: asyncpg.Connection, batch_id: UUID, *,
    status: Optional[str] = None,
    records_valid: Optional[int] = None,
    records_needing_review: Optional[int] = None,
    records_rejected: Optional[int] = None,
    records_published: Optional[int] = None,
    completed: bool = False,
) -> None:
    fields, values = [], []
    for column, value in (
        ("status", status), ("records_valid", records_valid),
        ("records_needing_review", records_needing_review),
        ("records_rejected", records_rejected), ("records_published", records_published),
    ):
        if value is not None:
            values.append(value)
            fields.append(f"{column} = ${len(values)}")
    if completed:
        fields.append("completed_at = now()")
    if not fields:
        return
    values.append(batch_id)
    await conn.execute(
        f"UPDATE catalog_import_batches SET {', '.join(fields)} WHERE id = ${len(values)}", *values,
    )


# ---------------------------------------------------------------------------
# catalog_import_records
# ---------------------------------------------------------------------------


async def get_or_create_import_record(
    conn: asyncpg.Connection, *, batch_id: UUID, external_record_id: str,
    raw_payload: Dict[str, Any], payload_sha256: str,
) -> tuple[Dict[str, Any], bool]:
    """Record-level idempotency WITHIN one batch: importing the file
    twice (e.g. a resumed/retried run) never creates a second row for
    the same external_record_id. `raw_payload` is written exactly once
    here and never overwritten by any other function in this module --
    that is what "immutable after receipt" actually means at the code
    level, not just a comment."""
    row = await conn.fetchrow(
        """
        INSERT INTO catalog_import_records
            (batch_id, external_record_id, raw_payload, payload_sha256, status)
        VALUES ($1, $2, $3::jsonb, $4, 'RECEIVED')
        ON CONFLICT (batch_id, external_record_id) DO NOTHING
        RETURNING *
        """,
        batch_id, external_record_id, json.dumps(raw_payload), payload_sha256,
    )
    if row is not None:
        return _decode_import_record(row), True
    existing = await conn.fetchrow(
        "SELECT * FROM catalog_import_records WHERE batch_id = $1 AND external_record_id = $2",
        batch_id, external_record_id,
    )
    return _decode_import_record(existing), False


def _decode_import_record(row: asyncpg.Record) -> Dict[str, Any]:
    record = dict(row)
    for field in ("raw_payload", "normalized_payload", "validation_errors", "review_reason_codes"):
        value = record.get(field)
        if isinstance(value, str):
            record[field] = json.loads(value)
    return record


async def get_import_record(pool: asyncpg.Pool, import_record_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow("SELECT * FROM catalog_import_records WHERE id = $1", import_record_id)
    return _decode_import_record(row) if row is not None else None


async def get_import_record_for_update(
    conn: asyncpg.Connection, import_record_id: UUID,
) -> Optional[Dict[str, Any]]:
    """`FOR UPDATE` -- must be the very first thing
    CatalogPublicationService.publish() does with this row (Blocker 4,
    independent review): two concurrent publish() calls for the SAME
    import_record_id both reading a plain, unlocked SELECT could both
    observe `status = 'VALIDATED'` and both proceed into catalog
    creation. Locking here serializes them -- the second caller blocks
    until the first's transaction commits (or rolls back), then reads
    whatever the first one left behind (typically `PUBLISHED`) and
    takes the idempotent-no-op branch instead of duplicating anything.
    Same pattern as get_current_formulation_for_update()'s own
    pre-existing lock on the *destination* row -- this is the missing
    lock on the *source* (staging) row."""
    row = await conn.fetchrow(
        "SELECT * FROM catalog_import_records WHERE id = $1 FOR UPDATE", import_record_id,
    )
    return _decode_import_record(row) if row is not None else None


async def list_import_records_for_batch(
    pool: asyncpg.Pool, batch_id: UUID, *, status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if status is not None:
        rows = await pool.fetch(
            "SELECT * FROM catalog_import_records WHERE batch_id = $1 AND status = $2 ORDER BY created_at",
            batch_id, status,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM catalog_import_records WHERE batch_id = $1 ORDER BY created_at", batch_id,
        )
    return [_decode_import_record(r) for r in rows]


async def update_import_record(
    conn: asyncpg.Connection, import_record_id: UUID, *,
    status: Optional[str] = None,
    normalized_payload: Optional[Dict[str, Any]] = None,
    validation_errors: Optional[List[Any]] = None,
    review_reason_codes: Optional[List[str]] = None,
    formulation_id: Optional[UUID] = None,
    superseded_formulation_id: Optional[UUID] = None,
) -> None:
    """Never touches `raw_payload` -- see get_or_create_import_record's
    own docstring. Every argument left as None is left unchanged, not
    nulled out; pass an explicit empty list/dict to clear a field."""
    fields, values = [], []

    def _add(column: str, value: Any, *, is_json: bool = False):
        if value is None:
            return
        values.append(json.dumps(value) if is_json else value)
        fields.append(f"{column} = ${len(values)}" + ("::jsonb" if is_json else ""))

    _add("status", status)
    _add("normalized_payload", normalized_payload, is_json=True)
    _add("validation_errors", validation_errors, is_json=True)
    _add("review_reason_codes", review_reason_codes, is_json=True)
    _add("formulation_id", formulation_id)
    _add("superseded_formulation_id", superseded_formulation_id)
    if not fields:
        return
    fields.append("updated_at = now()")
    values.append(import_record_id)
    await conn.execute(
        f"UPDATE catalog_import_records SET {', '.join(fields)} WHERE id = ${len(values)}", *values,
    )


# ---------------------------------------------------------------------------
# catalog_review_items
# ---------------------------------------------------------------------------


async def create_review_item(
    conn: asyncpg.Connection, *, import_record_id: UUID, reason_code: str,
    identity_key: Optional[str] = None,
) -> Dict[str, Any]:
    """`identity_key` distinguishes multiple independent instances of
    the same reason_code on the same record (today, only
    UNKNOWN_INGREDIENT needs this -- each unresolved raw ingredient
    name gets its own row). NULL for every single-instance reason
    code. See migration 154080b29153 and
    app/domain/catalog_validation.py."""
    row = await conn.fetchrow(
        """
        INSERT INTO catalog_review_items (import_record_id, reason_code, identity_key)
        VALUES ($1, $2, $3)
        RETURNING *
        """,
        import_record_id, reason_code, identity_key,
    )
    return dict(row)


async def find_review_item(
    conn: asyncpg.Connection, import_record_id: UUID, reason_code: str, identity_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Regardless of status -- an existing RESOLVED row for this exact
    (reason_code, identity_key) must never be recreated (that would
    silently re-open a problem a human already explicitly resolved).
    `IS NOT DISTINCT FROM` (not `=`) so two NULL identity_keys compare
    equal, matching ordinary SQL NULL semantics being wrong here."""
    row = await conn.fetchrow(
        """
        SELECT * FROM catalog_review_items
        WHERE import_record_id = $1 AND reason_code = $2 AND identity_key IS NOT DISTINCT FROM $3
        """,
        import_record_id, reason_code, identity_key,
    )
    return dict(row) if row is not None else None


async def count_open_review_items(conn: asyncpg.Connection, import_record_id: UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM catalog_review_items WHERE import_record_id = $1 AND status = 'OPEN'",
        import_record_id,
    )


async def get_review_item(pool: asyncpg.Pool, review_item_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow("SELECT * FROM catalog_review_items WHERE id = $1", review_item_id)
    return dict(row) if row is not None else None


async def list_review_items(
    pool: asyncpg.Pool, *, status: str = "OPEN", reason_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if reason_code is not None:
        rows = await pool.fetch(
            "SELECT * FROM catalog_review_items WHERE status = $1 AND reason_code = $2 ORDER BY created_at",
            status, reason_code,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM catalog_review_items WHERE status = $1 ORDER BY created_at", status,
        )
    return [dict(r) for r in rows]


async def resolve_review_item(
    conn: asyncpg.Connection, review_item_id: UUID, *,
    status: str, resolution: Optional[str], reviewed_by: str, resolution_notes: Optional[str] = None,
) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        UPDATE catalog_review_items
        SET status = $2, resolution = $3, resolution_notes = $4, reviewed_by = $5, reviewed_at = now()
        WHERE id = $1
        RETURNING *
        """,
        review_item_id, status, resolution, resolution_notes, reviewed_by,
    )
    return dict(row)


# ---------------------------------------------------------------------------
# catalog_formulation_provenance
# ---------------------------------------------------------------------------


async def create_provenance(
    conn: asyncpg.Connection, *, formulation_id: UUID, import_record_id: UUID,
    source_reference: Optional[str], verified_at: Optional[datetime], verification_actor: str,
    superseded_formulation_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO catalog_formulation_provenance
            (formulation_id, import_record_id, source_reference, verified_at,
             verification_actor, superseded_formulation_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        formulation_id, import_record_id, source_reference, verified_at,
        verification_actor, superseded_formulation_id,
    )
    return dict(row)


async def get_provenance_for_formulation(pool: asyncpg.Pool, formulation_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow(
        "SELECT * FROM catalog_formulation_provenance WHERE formulation_id = $1", formulation_id,
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# catalog_audit_log
# ---------------------------------------------------------------------------


async def write_audit(
    conn: asyncpg.Connection, *, action: str, entity_type: str, entity_id: Optional[UUID], actor: str,
    import_record_id: Optional[UUID] = None,
    before_metadata: Optional[Dict[str, Any]] = None,
    after_metadata: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """Small structured summaries only -- never a duplicated copy of a
    giant payload the immutable raw_payload already holds (see
    migration 2de8380d3618's docstring). Callers pass e.g.
    {"publication_status": "DRAFT"} -> {"publication_status":
    "PUBLISHED"}, not the whole normalized record."""
    await conn.execute(
        """
        INSERT INTO catalog_audit_log
            (action, entity_type, entity_id, import_record_id, actor, before_metadata, after_metadata, reason)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)
        """,
        action, entity_type, entity_id, import_record_id, actor,
        json.dumps(before_metadata) if before_metadata is not None else None,
        json.dumps(after_metadata) if after_metadata is not None else None,
        reason,
    )


async def list_audit_for_entity(pool: asyncpg.Pool, entity_type: str, entity_id: UUID) -> List[Dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT * FROM catalog_audit_log WHERE entity_type = $1 AND entity_id = $2 ORDER BY created_at",
        entity_type, entity_id,
    )
    results = []
    for r in rows:
        row = dict(r)
        for field in ("before_metadata", "after_metadata"):
            if isinstance(row.get(field), str):
                row[field] = json.loads(row[field])
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Production catalog writes (skincare_catalog_admin only)
# ---------------------------------------------------------------------------


async def resolve_or_create_brand(conn: asyncpg.Connection, name: str) -> tuple[UUID, bool]:
    """Deterministic identity resolution (Section 10): normalized exact
    match only, never fuzzy. Returns (brand_id, created).

    Concurrency-safe (Blocker 4, independent review): the original
    plain SELECT-then-INSERT had a real race -- two concurrent
    publications introducing the same previously-unseen brand could
    both see no existing row and both attempt the INSERT, one of them
    raising a raw `UniqueViolationError` on `brands.normalized_name`
    instead of resolving to the single logical brand. `INSERT ...
    ON CONFLICT (normalized_name) DO NOTHING` never raises on that
    race -- Postgres's own speculative-insertion protocol makes a
    concurrent conflicting insert wait for the other transaction to
    finish rather than error, so the loser simply gets no row back
    here and the reselect below finds whichever row actually won."""
    normalized = normalize_name(name)
    row = await conn.fetchrow(
        "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) "
        "ON CONFLICT (normalized_name) DO NOTHING RETURNING id",
        name, normalized,
    )
    if row is not None:
        return row["id"], True
    existing = await conn.fetchval("SELECT id FROM brands WHERE normalized_name = $1", normalized)
    return existing, False


async def resolve_or_create_product(
    conn: asyncpg.Connection, *, brand_id: UUID, name: str, category: str, description: Optional[str] = None,
) -> tuple[UUID, bool]:
    """Deterministic identity resolution, same concurrency-safe
    ON-CONFLICT-then-reselect pattern as resolve_or_create_brand()
    above, against `products`' own `UNIQUE (brand_id, normalized_name)`
    constraint (migration d70e5fc90775). `brand_id`+`normalized_name`
    being unique also means this can only ever resolve to zero or one
    row -- product identity ambiguity (PRODUCT_IDENTITY_AMBIGUOUS)
    arises from a *different* signal this pass's exact-match path
    structurally cannot produce (e.g. a future fuzzy-suggestion admin
    tool), never from this query itself. Returns (product_id,
    created); `category`/`description` are only used on the creating
    call -- an existing product's own values are never overwritten by
    a later import that happens to describe it slightly differently
    (that is a deliberate, separate decision this pass does not make
    casually, matching resolve_or_create_brand's own "identity
    resolution only, never a silent data overwrite" posture)."""
    normalized = normalize_name(name)
    row = await conn.fetchrow(
        """
        INSERT INTO products (brand_id, name, normalized_name, category, description)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (brand_id, normalized_name) DO NOTHING
        RETURNING id
        """,
        brand_id, name, normalized, category, description,
    )
    if row is not None:
        return row["id"], True
    existing = await conn.fetchval(
        "SELECT id FROM products WHERE brand_id = $1 AND normalized_name = $2", brand_id, normalized,
    )
    return existing, False


async def get_current_formulation_for_update(
    conn: asyncpg.Connection, product_id: UUID, market_or_region: str,
) -> Optional[Dict[str, Any]]:
    """`FOR UPDATE` -- must run inside the same transaction that may go
    on to supersede this row, so two concurrent publications for the
    same product/market serialize on this lock rather than both
    deciding, from a stale read, that they're the one creating the new
    current formulation (Section 20's "two new formulations same
    product/market -> unique-current invariant preserved")."""
    row = await conn.fetchrow(
        """
        SELECT * FROM product_formulations
        WHERE product_id = $1 AND market_or_region = $2 AND is_current = true
        FOR UPDATE
        """,
        product_id, market_or_region,
    )
    return dict(row) if row is not None else None


async def create_draft_formulation(
    conn: asyncpg.Connection, *, product_id: UUID, version: str, market_or_region: str,
    source_type: str, source_reference: Optional[str], effective_from: Optional[date],
) -> UUID:
    """Always created `is_current = false` / `publication_status =
    'DRAFT'` / `ingredient_data_status = 'UNKNOWN'` -- publication is a
    separate, explicit, later step (CatalogPublicationService.publish),
    never implied by mere creation."""
    return await conn.fetchval(
        """
        INSERT INTO product_formulations
            (product_id, version, market_or_region, source_type, source_reference,
             effective_from, is_current, publication_status, ingredient_data_status)
        VALUES ($1, $2, $3, $4, $5, $6, false, 'DRAFT', 'UNKNOWN')
        RETURNING id
        """,
        product_id, version, market_or_region, source_type, source_reference, effective_from,
    )


async def attach_formulation_ingredients(
    conn: asyncpg.Connection, formulation_id: UUID, resolved_ingredients: Sequence[Dict[str, Any]],
) -> None:
    """`resolved_ingredients` items carry `ingredient_id`/`position`/
    `declared_concentration`/`concentration_unit` -- resolution
    (mapping a raw_name to an ingredient_id) has already happened by
    this point (app/domain/catalog_ingredient_resolution.py); this
    function only ever writes rows whose ingredient_id is already a
    real, resolved `ingredients.id`."""
    for item in resolved_ingredients:
        await conn.execute(
            """
            INSERT INTO formulation_ingredients
                (formulation_id, ingredient_id, position, declared_concentration, concentration_unit, notes)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            formulation_id, item["ingredient_id"], item["position"],
            item.get("declared_concentration"), item.get("concentration_unit"), item.get("notes"),
        )


async def find_conflicting_skus(
    conn: asyncpg.Connection, sku_values: Sequence[str], *, exclude_product_id: Optional[UUID] = None,
) -> List[Dict[str, Any]]:
    """A SKU value that already exists in the catalog under a
    DIFFERENT product (or under ANY product, when the record's own
    product doesn't exist yet -- `exclude_product_id=None`) is a real
    conflict (Section 2's SKU_CONFLICT) -- never silently overwritten
    or reassigned. A SKU that already exists under THIS SAME product
    (e.g. republishing after a reformulation, same packaging/SKU code
    carried forward) is not a conflict; the caller re-points it at the
    new formulation instead of inserting a duplicate."""
    if not sku_values:
        return []
    if exclude_product_id is not None:
        rows = await conn.fetch(
            "SELECT * FROM product_skus WHERE sku = ANY($1::varchar[]) AND product_id <> $2",
            list(sku_values), exclude_product_id,
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM product_skus WHERE sku = ANY($1::varchar[])", list(sku_values),
        )
    return [dict(r) for r in rows]


async def upsert_sku_for_formulation(
    conn: asyncpg.Connection, *, product_id: UUID, formulation_id: UUID, sku: str,
    upc_or_ean: Optional[str], size_value: Optional[float], size_unit: Optional[str],
    market_or_region: str,
) -> None:
    """`ON CONFLICT (product_id, sku)` re-points an existing SKU at the
    new formulation (the reformulation case: same physical packaging
    code, new formula) rather than failing or duplicating -- the
    conflict this guards against (a DIFFERENT product claiming the same
    SKU) is checked separately, before publication ever reaches this
    call, by find_conflicting_skus()."""
    await conn.execute(
        """
        INSERT INTO product_skus
            (product_id, formulation_id, sku, upc_or_ean, size_value, size_unit, market_or_region)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (product_id, sku) DO UPDATE SET
            formulation_id = EXCLUDED.formulation_id,
            upc_or_ean = EXCLUDED.upc_or_ean,
            size_value = EXCLUDED.size_value,
            size_unit = EXCLUDED.size_unit,
            market_or_region = EXCLUDED.market_or_region,
            updated_at = now()
        """,
        product_id, formulation_id, sku, upc_or_ean, size_value, size_unit, market_or_region,
    )


async def set_formulation_publication_state(
    conn: asyncpg.Connection, formulation_id: UUID, *,
    publication_status: str,
    ingredient_data_status: Optional[str] = None,
    is_current: Optional[bool] = None,
    verified_at: Optional[datetime] = None,
) -> None:
    fields = ["publication_status = $2", "updated_at = now()"]
    values: List[Any] = [formulation_id, publication_status]
    if ingredient_data_status is not None:
        values.append(ingredient_data_status)
        fields.append(f"ingredient_data_status = ${len(values)}")
    if is_current is not None:
        values.append(is_current)
        fields.append(f"is_current = ${len(values)}")
    if verified_at is not None:
        values.append(verified_at)
        fields.append(f"verified_at = ${len(values)}")
    await conn.execute(f"UPDATE product_formulations SET {', '.join(fields)} WHERE id = $1", *values)


async def supersede_formulation(conn: asyncpg.Connection, formulation_id: UUID) -> None:
    """Never deletes -- a superseded formulation stays in the table
    forever (Section 11: "Do not delete formulation A. Historical
    analyses may have referenced it")."""
    await conn.execute(
        "UPDATE product_formulations SET is_current = false, publication_status = 'SUPERSEDED', "
        "updated_at = now() WHERE id = $1",
        formulation_id,
    )


async def create_canonical_ingredient(
    conn: asyncpg.Connection, *, canonical_name: str, ingredient_type: Optional[str] = None,
    inci_name: Optional[str] = None,
) -> UUID:
    """Caller (app/domain/catalog_ingredient_resolution.py) must
    already have confirmed no existing ingredient/alias resolves to
    this name before calling -- this function does not itself
    re-check, so a UNIQUE-violation here is a genuine concurrent-
    creation race, not an expected outcome (see Section 20's
    concurrency requirements; callers run this inside the same
    transaction as their own pre-check under `FOR UPDATE`-equivalent
    care, or accept the race as a real, surfaced error rather than
    silently swallowing it)."""
    return await conn.fetchval(
        """
        INSERT INTO ingredients (canonical_name, normalized_name, inci_name, ingredient_type)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        canonical_name, normalize_name(canonical_name), inci_name, ingredient_type,
    )


async def find_alias_conflict(conn: asyncpg.Connection, alias: str) -> Optional[Dict[str, Any]]:
    """Section 17's pre-insert conflict check: does this normalized
    alias already mean something? Checks both directions -- already a
    canonical ingredient's own name, or already someone else's alias --
    and returns enough detail (`conflict_type`, `ingredient_id`) for
    the caller to fail closed with a specific, actionable reason rather
    than a bare IntegrityError."""
    normalized = normalize_name(alias)
    canonical_row = await conn.fetchrow(
        "SELECT id, canonical_name FROM ingredients WHERE normalized_name = $1", normalized,
    )
    if canonical_row is not None:
        return {"conflict_type": "IS_CANONICAL_NAME", "ingredient_id": canonical_row["id"],
                "ingredient_name": canonical_row["canonical_name"]}
    alias_row = await conn.fetchrow(
        """
        SELECT a.ingredient_id, i.canonical_name
        FROM ingredient_aliases a JOIN ingredients i ON i.id = a.ingredient_id
        WHERE a.normalized_alias = $1
        """,
        normalized,
    )
    if alias_row is not None:
        return {"conflict_type": "IS_EXISTING_ALIAS", "ingredient_id": alias_row["ingredient_id"],
                "ingredient_name": alias_row["canonical_name"]}
    return None


async def create_ingredient_alias(
    conn: asyncpg.Connection, *, ingredient_id: UUID, alias: str, alias_type: str = "synonym",
) -> UUID:
    """Caller must have already called find_alias_conflict() and
    confirmed no conflict (or that the only "conflict" is this exact
    alias already mapping to this same ingredient_id, which is a
    harmless no-op the caller should short-circuit on) -- this function
    does not re-check, matching create_canonical_ingredient()'s own
    contract."""
    return await conn.fetchval(
        """
        INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias, alias_type)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        ingredient_id, alias, normalize_name(alias), alias_type,
    )
