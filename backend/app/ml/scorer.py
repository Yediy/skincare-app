from typing import Dict, List, Any, Tuple
from app.domain.priorities import PRIORITIES, PriorityDef
import logging

logger = logging.getLogger(__name__)


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
        region_metrics: Dict[str, Any],
        capture_quality: float = 1.0,
        historical_priorities: Dict[str, int] = None
    ) -> Dict[str, Any]:
        historical_priorities = historical_priorities or {}

        insights = {
            "skin_health": self._analyze_skin_health(region_metrics, capture_quality, historical_priorities),
            "vitality": self._analyze_vitality(region_metrics, capture_quality, historical_priorities),
            "feature_definition": self._analyze_features(region_metrics, capture_quality, historical_priorities),
            "facial_harmony": self._analyze_harmony(region_metrics, capture_quality, historical_priorities),
        }

        scores = self._calculate_pillar_scores(region_metrics)

        return {
            "scores": scores,
            "insights": insights,
            "capture_quality": capture_quality
        }

    def _analyze_skin_health(self, metrics, confidence, historical):
        priority_ids = []
        priority_severities = {}
        influencers = {}

        skin_priorities = [p for p in PRIORITIES.values() if p.pillar == "skin_health"]

        for priority_def in skin_priorities:
            metric_value = metrics.get(priority_def.metric_key, 0.5)
            historical_count = historical.get(priority_def.id, 0)

            triggered, severity = self.trigger_helper.should_trigger(
                priority_def, metric_value, confidence, historical_count
            )

            if triggered:
                priority_ids.append(priority_def.id)
                priority_severities[priority_def.id] = severity
                influencers[priority_def.id] = {
                    "metric_value": metric_value,
                    "threshold": priority_def.severity_threshold,
                    "direction": priority_def.direction,
                    "severity": severity,
                    "confidence": confidence
                }

        return {
            "priority_ids": priority_ids,
            "priority_severities": priority_severities,
            "influencers": influencers,
            "overall_score": self._compute_skin_health_score(metrics)
        }

    def _analyze_vitality(self, metrics, confidence, historical):
        priority_ids = []
        priority_severities = {}
        influencers = {}

        vitality_priorities = [p for p in PRIORITIES.values() if p.pillar == "vitality"]

        for priority_def in vitality_priorities:
            metric_value = metrics.get(priority_def.metric_key, 0.5)
            historical_count = historical.get(priority_def.id, 0)

            triggered, severity = self.trigger_helper.should_trigger(
                priority_def, metric_value, confidence, historical_count
            )

            if triggered:
                priority_ids.append(priority_def.id)
                priority_severities[priority_def.id] = severity
                influencers[priority_def.id] = {
                    "metric_value": metric_value,
                    "severity": severity
                }

        return {
            "priority_ids": priority_ids,
            "priority_severities": priority_severities,
            "influencers": influencers,
            "overall_score": self._compute_vitality_score(metrics)
        }

    def _analyze_features(self, metrics, confidence, historical):
        priority_ids = []
        priority_severities = {}
        influencers = {}

        feature_priorities = [p for p in PRIORITIES.values() if p.pillar == "feature_definition"]

        for priority_def in feature_priorities:
            metric_value = metrics.get(priority_def.metric_key, 0.7)
            historical_count = historical.get(priority_def.id, 0)

            triggered, severity = self.trigger_helper.should_trigger(
                priority_def, metric_value, confidence, historical_count
            )

            if triggered:
                priority_ids.append(priority_def.id)
                priority_severities[priority_def.id] = severity
                influencers[priority_def.id] = {
                    "metric_value": metric_value,
                    "severity": severity
                }

        return {
            "priority_ids": priority_ids,
            "priority_severities": priority_severities,
            "influencers": influencers,
            "overall_score": metrics.get("feature_definition_score", 0.7)
        }

    def _analyze_harmony(self, metrics, confidence, historical):
        priority_ids = []
        priority_severities = {}
        influencers = {}

        harmony_priorities = [p for p in PRIORITIES.values() if p.pillar == "facial_harmony"]

        for priority_def in harmony_priorities:
            metric_value = metrics.get(priority_def.metric_key, 0.8)
            historical_count = historical.get(priority_def.id, 0)

            triggered, severity = self.trigger_helper.should_trigger(
                priority_def, metric_value, confidence, historical_count
            )

            if triggered:
                priority_ids.append(priority_def.id)
                priority_severities[priority_def.id] = severity
                influencers[priority_def.id] = {
                    "metric_value": metric_value,
                    "severity": severity
                }

        return {
            "priority_ids": priority_ids,
            "priority_severities": priority_severities,
            "influencers": influencers,
            "overall_score": metrics.get("symmetry_score", 0.8)
        }

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
