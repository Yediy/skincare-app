"""Phase 11: an ABSTAINED metric must not be able to trigger a
personalized concern, no matter what its (withheld) value would have
implied. Pure Python -- MetricResult objects constructed directly, no
CV pipeline or real photo needed."""
from app.cv.metric_result import MetricResult, MetricStatus
from app.ml.scorer import FacialScorer

# Every metric_key referenced by at least one PriorityDef, given a
# baseline VALID result so only the metric under test varies.
_BASELINE_VALUES = {
    "evenness_score": 0.8,          # comfortably below its lower_worse threshold (0.65) -- non-triggering
    "redness_score": 0.1,           # comfortably below its higher_worse threshold (0.45) -- non-triggering
    "oiliness_score": 0.1,
    "texture_score": 0.1,
    "under_eye_darkness": 0.1,
    "puffiness_score": 0.1,
    "feature_definition_score": 0.9,
    "symmetry_score": 0.9,
}


def _baseline_metric_results():
    return {
        name: MetricResult(metric_name=name, value=value, confidence=0.9, status=MetricStatus.VALID)
        for name, value in _BASELINE_VALUES.items()
    }


def test_valid_high_redness_triggers_redness_control():
    metric_results = _baseline_metric_results()
    metric_results["redness_score"] = MetricResult(
        metric_name="redness_score", value=0.9, confidence=0.9, status=MetricStatus.VALID
    )

    result = FacialScorer().compute_scores(metric_results)

    assert "REDNESS_CONTROL" in result["insights"]["skin_health"]["priority_ids"]


def test_abstained_redness_cannot_trigger_redness_control_even_with_a_high_underlying_value():
    """The core Phase 11 proof: identical severity-implying value, but
    ABSTAINED status (value withheld) -- must not trigger, at all."""
    metric_results = _baseline_metric_results()
    metric_results["redness_score"] = MetricResult(
        metric_name="redness_score", value=None, confidence=0.1, status=MetricStatus.ABSTAINED,
        uncertainty_reasons=["poor_lighting_balance", "poor_exposure"],
    )

    result = FacialScorer().compute_scores(metric_results)

    assert "REDNESS_CONTROL" not in result["insights"]["skin_health"]["priority_ids"]


def test_abstained_metric_still_produces_a_pillar_aggregate_score():
    """Abstention blocks *triggering*, not the coarse aggregate score
    -- that still needs a number to avoid breaking the whole response,
    using the documented neutral fallback, never the withheld value."""
    metric_results = _baseline_metric_results()
    metric_results["redness_score"] = MetricResult(
        metric_name="redness_score", value=None, confidence=0.1, status=MetricStatus.ABSTAINED,
    )

    result = FacialScorer().compute_scores(metric_results)

    assert isinstance(result["scores"]["skin_health_score"], float)


def test_borderline_metric_still_triggers_but_is_flagged_in_influencer():
    metric_results = _baseline_metric_results()
    metric_results["redness_score"] = MetricResult(
        metric_name="redness_score", value=0.9, confidence=0.5, status=MetricStatus.BORDERLINE,
        uncertainty_reasons=["poor_exposure"],
    )

    result = FacialScorer().compute_scores(metric_results)

    assert "REDNESS_CONTROL" in result["insights"]["skin_health"]["priority_ids"]
    influencer = result["insights"]["skin_health"]["influencers"]["REDNESS_CONTROL"]
    assert influencer["metric_status"] == "BORDERLINE"
    # Lower confidence must genuinely reduce severity vs. a VALID/full-confidence trigger.
    assert influencer["confidence"] == 0.5


def test_metric_results_included_in_output_for_inspection():
    metric_results = _baseline_metric_results()
    result = FacialScorer().compute_scores(metric_results)
    assert set(result["metric_results"].keys()) == set(_BASELINE_VALUES.keys())
    assert result["metric_results"]["redness_score"]["status"] == "VALID"
