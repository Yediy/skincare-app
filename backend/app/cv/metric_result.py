"""
Common result structure for every skin/face metric -- replacing the
bare floats every one of the 8 metrics used to return with no
confidence signal and no way to say "I can't measure this reliably
from this photo."

`confidence` is explicitly a measurement-reliability score (how much
the current capture conditions corrupt this specific metric), not a
clinical probability of any kind -- see CV_VALIDATION_LIMITATIONS.md.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

METRIC_VERSION = "1.0"
# "uncalibrated" is the honest label here -- there is no validated
# calibration dataset behind any of these formulas (see
# CV_VALIDATION_LIMITATIONS.md). Do not rename this to imply otherwise
# without an actual calibration process behind it.
CALIBRATION_VERSION = "uncalibrated-1.0"


class MetricStatus(str, Enum):
    VALID = "VALID"
    BORDERLINE = "BORDERLINE"
    ABSTAINED = "ABSTAINED"


@dataclass
class MetricResult:
    metric_name: str
    value: Optional[float]  # None when status == ABSTAINED
    confidence: float
    status: MetricStatus
    uncertainty_reasons: List[str] = field(default_factory=list)
    metric_version: str = METRIC_VERSION
    calibration_version: str = CALIBRATION_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "value": self.value,
            "confidence": self.confidence,
            "status": self.status.value,
            "uncertainty_reasons": self.uncertainty_reasons,
            "metric_version": self.metric_version,
            "calibration_version": self.calibration_version,
        }
