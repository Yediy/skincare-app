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
