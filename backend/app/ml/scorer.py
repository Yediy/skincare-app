from typing import Dict, List, Any, Tuple
from app.domain.priorities import PRIORITIES, PriorityDef
from app.cv.metric_result import MetricResult, MetricStatus
import logging

logger = logging.getLogger(__name__)

# Used only for the coarse pillar-aggregate scores (_compute_skin_health_score
# etc.) when a metric has abstained -- never for triggering a specific
# concern. An abstained metric is skipped entirely in the per-priority
# loops below (Phase 11); this fallback exists purely so one abstained
# input doesn't crash a 4-metric aggregate average.
_ABSTAINED_AGGREGATE_FALLBACK = 0.5


class PriorityTrigger:
    @staticmethod
    def should_trigger(
        priority_def: PriorityDef,
        metric_value: float,
        confidence: float = 1.0,
        historical_detections: int = 0
    ) -> Tuple[bool, float]:
        threshold = priority_def.severity_threshold

        if priority_def.direction == "higher_worse":
            triggered = metric_value > threshold
            severity = max(0.0, metric_value - threshold)
        else:
            triggered = metric_value < threshold
            severity = max(0.0, threshold - metric_value)

        severity = severity * confidence

        if historical_detections > 0:
            persistence_multiplier = 1.0 + (priority_def.persistence_bonus * min(historical_detections, 3))
            severity = severity * persistence_multiplier

        return triggered, severity


class FacialScorer:
    def __init__(self):
        self.trigger_helper = PriorityTrigger()

    def compute_scores(
        self,
        metric_results: Dict[str, MetricResult],
        capture_quality: float = 1.0,
        historical_priorities: Dict[str, int] = None
    ) -> Dict[str, Any]:
        """
        metric_results: metric_key -> MetricResult (Phase 9), each
        carrying its own confidence (Phase 10) -- no longer a single
        blanket `capture_quality` applied identically to all 8 metrics.
        An ABSTAINED metric is excluded from priority triggering
        entirely (Phase 11): it cannot create a personalized concern,
        no matter how the aggregate pillar score below happens to land.
        """
        historical_priorities = historical_priorities or {}

        # Plain values for the coarse pillar-aggregate math only.
        plain_metrics = {
            name: (mr.value if mr.status != MetricStatus.ABSTAINED else _ABSTAINED_AGGREGATE_FALLBACK)
            for name, mr in metric_results.items()
        }

        insights = {
            "skin_health": self._analyze_pillar("skin_health", metric_results, plain_metrics, historical_priorities),
            "vitality": self._analyze_pillar("vitality", metric_results, plain_metrics, historical_priorities),
            "feature_definition": self._analyze_pillar("feature_definition", metric_results, plain_metrics, historical_priorities),
            "facial_harmony": self._analyze_pillar("facial_harmony", metric_results, plain_metrics, historical_priorities),
        }

        scores = self._calculate_pillar_scores(plain_metrics)

        return {
            "scores": scores,
            "insights": insights,
            "capture_quality": capture_quality,
            "metric_results": {name: mr.to_dict() for name, mr in metric_results.items()},
        }

    def _analyze_pillar(self, pillar_name, metric_results, plain_metrics, historical):
        priority_ids = []
        priority_severities = {}
        influencers = {}

        pillar_priorities = [p for p in PRIORITIES.values() if p.pillar == pillar_name]

        for priority_def in pillar_priorities:
            metric_result = metric_results.get(priority_def.metric_key)

            # The core Phase 11 behavior: an abstained metric cannot
            # trigger -- not "trigger with low confidence", not
            # "trigger using a fallback value" -- excluded outright.
            if metric_result is not None and metric_result.status == MetricStatus.ABSTAINED:
                continue

            metric_value = plain_metrics.get(priority_def.metric_key, 0.5)
            metric_confidence = metric_result.confidence if metric_result is not None else 1.0
            historical_count = historical.get(priority_def.id, 0)

            triggered, severity = self.trigger_helper.should_trigger(
                priority_def, metric_value, metric_confidence, historical_count
            )

            if triggered:
                priority_ids.append(priority_def.id)
                priority_severities[priority_def.id] = severity
                influencers[priority_def.id] = {
                    "metric_value": metric_value,
                    "threshold": priority_def.severity_threshold,
                    "direction": priority_def.direction,
                    "severity": severity,
                    "confidence": metric_confidence,
                    "metric_status": metric_result.status.value if metric_result is not None else "UNKNOWN",
                }

        return {
            "priority_ids": priority_ids,
            "priority_severities": priority_severities,
            "influencers": influencers,
            "overall_score": self._pillar_overall_score(pillar_name, plain_metrics),
        }

    def _pillar_overall_score(self, pillar_name, metrics):
        if pillar_name == "skin_health":
            return self._compute_skin_health_score(metrics)
        if pillar_name == "vitality":
            return self._compute_vitality_score(metrics)
        if pillar_name == "feature_definition":
            return metrics.get("feature_definition_score", 0.7)
        if pillar_name == "facial_harmony":
            return metrics.get("symmetry_score", 0.8)
        return 0.5

    def _calculate_pillar_scores(self, metrics):
        return {
            "skin_health_score": self._compute_skin_health_score(metrics),
            "vitality_score": self._compute_vitality_score(metrics),
            "feature_definition_score": metrics.get("feature_definition_score", 0.7),
            "facial_harmony_score": metrics.get("symmetry_score", 0.8),
        }

    def _compute_skin_health_score(self, metrics):
        return (
            metrics.get("evenness_score", 0.5) +
            (1 - metrics.get("redness_score", 0.5)) +
            (1 - metrics.get("oiliness_score", 0.5)) +
            (1 - metrics.get("texture_score", 0.5))
        ) / 4

    def _compute_vitality_score(self, metrics):
        return (
            (1 - metrics.get("under_eye_darkness", 0.5)) +
            (1 - metrics.get("puffiness_score", 0.5))
        ) / 2
