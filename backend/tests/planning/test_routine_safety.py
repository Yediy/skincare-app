"""ROUTINE safety evaluation (Part I, Phase 7/8) --
SafetyEngine.evaluate_routine_safety(), distinct from FORMULATION
safety (test_formulation_safety.py): can these formulations be used
together, in this proposed schedule? Against the real, synthetic
catalog (tests/conftest.py's synthetic_catalog fixture).
"""
import pytest

from app.domain.safety_engine import (
    ACTIVE_INTERACTION_CONFLICT,
    BARRIER_RECOVERY_CONFLICT,
    MAX_FREQUENCY_EXCEEDED,
    ProposedRoutineEntry,
    RESTRICTED,
    SAFE,
    SafetyEngine,
    SafetyUsageContext,
    UNSAFE,
)

INACTIVE_CONTEXT = SafetyUsageContext(barrier_recovery_active=False)
ACTIVE_BARRIER_CONTEXT = SafetyUsageContext(barrier_recovery_active=True)


async def test_routine_with_no_issues_is_safe(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["moisturizer"],
            proposed_weekly_frequency=7,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert decision.status == SAFE
    assert decision.allowed is True


async def test_max_frequency_not_exceeded_when_schedule_within_cap(app_db_pool, synthetic_catalog):
    """The retinol formulation's MAX_FREQUENCY rule caps at 3/week
    (see synthetic_catalog) -- proposing 2/week must not trigger
    MAX_FREQUENCY_EXCEEDED."""
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["retinol"],
            proposed_weekly_frequency=2,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert MAX_FREQUENCY_EXCEEDED not in decision.reason_codes
    assert decision.status == SAFE


async def test_max_frequency_exceeded_only_after_actual_schedule_comparison(app_db_pool, synthetic_catalog):
    """Proposing 5/week against a real cap of 3/week must trigger
    MAX_FREQUENCY_EXCEEDED -- this is the one and only place that
    reason code can be produced (never at the formulation level, see
    test_formulation_safety.py::test_max_frequency_rule_is_surfaced_as_restriction_not_exceeded)."""
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["retinol"],
            proposed_weekly_frequency=5,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert MAX_FREQUENCY_EXCEEDED in decision.reason_codes
    exceeded = decision.restrictions["frequency_caps_exceeded"]
    assert exceeded[0]["maximum_weekly_frequency"] == 3
    assert exceeded[0]["proposed_weekly_frequency"] == 5
    # The retinol rule's action is RESTRICT (see synthetic_catalog),
    # not EXCLUDE -- exceeding it is a fixable restriction (Phase 12's
    # actual routine must obey it), not an outright UNSAFE routine.
    assert decision.status == RESTRICTED
    assert decision.allowed is True


async def test_barrier_recovery_inactive_context_never_triggers_conflict(app_db_pool, synthetic_catalog):
    """The acid formulation has a real BARRIER_RECOVERY rule (see
    synthetic_catalog) -- with barrier_recovery_active=False (the
    default, never-invented state), it must never produce
    BARRIER_RECOVERY_CONFLICT."""
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["acid"],
            proposed_weekly_frequency=1,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert BARRIER_RECOVERY_CONFLICT not in decision.reason_codes
    assert decision.status == SAFE


async def test_barrier_recovery_active_context_triggers_conflict(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["acid"],
            proposed_weekly_frequency=1,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, ACTIVE_BARRIER_CONTEXT)
    assert BARRIER_RECOVERY_CONFLICT in decision.reason_codes
    assert str(synthetic_catalog["formulations"]["acid"]) in decision.restrictions["barrier_recovery_conflicts"]
    # The acid formulation's BARRIER_RECOVERY rule action is RESTRICT,
    # not EXCLUDE.
    assert decision.status == RESTRICTED


async def test_cross_product_exclude_combination_marks_routine_unsafe(app_db_pool, synthetic_catalog):
    """retinol + glycolic acid, from two DIFFERENT formulations (not
    the combined "Dual Active" product), have a real seeded
    EXCLUDE_COMBINATION interaction -- the routine combining them must
    be UNSAFE, not merely restricted."""
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["retinol"],
            proposed_weekly_frequency=2,
        ),
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["acid"],
            proposed_weekly_frequency=1,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert ACTIVE_INTERACTION_CONFLICT in decision.reason_codes
    assert decision.status == UNSAFE
    assert decision.allowed is False
    interactions = decision.restrictions["interactions"]
    assert any(i["recommended_action"] == "EXCLUDE_COMBINATION" for i in interactions)


async def test_cross_product_separate_daypart_is_restricted_not_unsafe(app_db_pool, synthetic_catalog):
    """niacinamide + glycolic acid (from "safe_serum" and "acid", two
    different formulations) have a real seeded SEPARATE_DAYPART
    interaction -- schedule-adjustable, so the routine is RESTRICTED
    (the routine builder must satisfy it), not UNSAFE."""
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["safe_serum"],
            proposed_weekly_frequency=7,
        ),
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["acid"],
            proposed_weekly_frequency=1,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert ACTIVE_INTERACTION_CONFLICT in decision.reason_codes
    assert decision.status == RESTRICTED
    assert decision.allowed is True
    interactions = decision.restrictions["interactions"]
    assert any(i["recommended_action"] == "SEPARATE_DAYPART" for i in interactions)


async def _add_formulation_with_rule(db_pool, brand_id, *, category, rule_type, action, parameters=None):
    """Test-local catalog insert -- a fresh ingredient/product/
    formulation carrying exactly one EXCLUDE-or-not rule of the given
    type, isolated from synthetic_catalog's own shared rows (which
    other tests already assert specific RESTRICT-action behavior on;
    this pass's new EXCLUDE-action coverage must not perturb those)."""
    import json as _json

    async with db_pool.acquire() as conn:
        ingredient_id = await conn.fetchval(
            "INSERT INTO ingredients (canonical_name, normalized_name, ingredient_type) "
            "VALUES ($1, $2, 'active') RETURNING id",
            f"Test Exclude Ingredient {rule_type} {action}", f"test exclude ingredient {rule_type} {action}".lower(),
        )
        product_id = await conn.fetchval(
            "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, $2, $3, $4) RETURNING id",
            brand_id, f"Test {rule_type} {action} Product", f"test {rule_type} {action} product".lower(), category,
        )
        formulation_id = await conn.fetchval(
            """
            INSERT INTO product_formulations
                (product_id, version, source_type, verified_at, ingredient_data_status, market_or_region)
            VALUES ($1, '1', 'manufacturer_disclosure', now(), 'COMPLETE', 'global')
            RETURNING id
            """,
            product_id,
        )
        await conn.execute(
            "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, $3)",
            product_id, formulation_id, f"TEST-{rule_type}-{action}-{formulation_id}",
        )
        await conn.execute(
            "INSERT INTO formulation_ingredients (formulation_id, ingredient_id, position) VALUES ($1, $2, 1)",
            formulation_id, ingredient_id,
        )
        await conn.execute(
            "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code, parameters) "
            "VALUES ($1, $2, 'HIGH', $3, $4, $5::jsonb)",
            ingredient_id, rule_type, action, f"{rule_type}_TEST", _json.dumps(parameters or {}),
        )
    return ingredient_id, product_id, formulation_id


async def test_max_frequency_exclude_action_marks_routine_unsafe_and_attributes_formulation(
    db_pool, synthetic_catalog,
):
    """A MAX_FREQUENCY rule whose action is EXCLUDE (not RESTRICT, see
    test_max_frequency_exceeded_only_after_actual_schedule_comparison
    for the RESTRICT case) must mark the routine UNSAFE once the
    proposed schedule exceeds the cap, and must record the offending
    formulation in exclude_action_formulation_ids so the routine
    builder (recommendation_service) can attribute and drop it."""
    _, _, formulation_id = await _add_formulation_with_rule(
        db_pool, synthetic_catalog["brand_id"], category="strict_retinoid",
        rule_type="MAX_FREQUENCY", action="EXCLUDE", parameters={"maximum_weekly_frequency": 1},
    )
    engine = SafetyEngine()
    entries = [ProposedRoutineEntry(formulation_id=formulation_id, proposed_weekly_frequency=7)]
    decision = await engine.evaluate_routine_safety(db_pool, entries, INACTIVE_CONTEXT)
    assert decision.status == UNSAFE
    assert decision.allowed is False
    assert MAX_FREQUENCY_EXCEEDED in decision.reason_codes
    assert str(formulation_id) in decision.restrictions["exclude_action_formulation_ids"]


async def test_barrier_recovery_exclude_action_marks_routine_unsafe_and_attributes_formulation(
    db_pool, synthetic_catalog,
):
    """Same as above, for a BARRIER_RECOVERY rule whose action is
    EXCLUDE, only while barrier_recovery_active is True (inert
    otherwise, same as the pre-existing RESTRICT-action test)."""
    _, _, formulation_id = await _add_formulation_with_rule(
        db_pool, synthetic_catalog["brand_id"], category="strict_barrier",
        rule_type="BARRIER_RECOVERY", action="EXCLUDE",
    )
    engine = SafetyEngine()
    entries = [ProposedRoutineEntry(formulation_id=formulation_id, proposed_weekly_frequency=1)]

    inactive_decision = await engine.evaluate_routine_safety(db_pool, entries, INACTIVE_CONTEXT)
    assert BARRIER_RECOVERY_CONFLICT not in inactive_decision.reason_codes
    assert inactive_decision.status == SAFE

    active_decision = await engine.evaluate_routine_safety(db_pool, entries, ACTIVE_BARRIER_CONTEXT)
    assert active_decision.status == UNSAFE
    assert active_decision.allowed is False
    assert str(formulation_id) in active_decision.restrictions["exclude_action_formulation_ids"]


async def test_single_formulation_routine_has_no_cross_product_interaction(app_db_pool, synthetic_catalog):
    """A routine with only one formulation can never produce a
    cross-product interaction finding -- get_interactions_within()
    needs at least two distinct ingredient IDs to find a pair at all."""
    engine = SafetyEngine()
    entries = [
        ProposedRoutineEntry(
            formulation_id=synthetic_catalog["formulations"]["retinol"],
            proposed_weekly_frequency=2,
        ),
    ]
    decision = await engine.evaluate_routine_safety(app_db_pool, entries, INACTIVE_CONTEXT)
    assert ACTIVE_INTERACTION_CONFLICT not in decision.reason_codes
