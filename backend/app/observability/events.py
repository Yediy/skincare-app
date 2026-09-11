"""Minimum observability (Part VIII of this pass): structured,
correlation-bearing log events for the async analysis pipeline.

Structured *events* (one stable event name + explicit structured
fields per log line), not a Prometheus/metrics-client integration --
this repository has no metrics backend deployed yet, and standing one
up is out of scope for this pass (same "don't prematurely deploy
infrastructure" posture app/queue/base.py's own docstring documents
for the job queue). Every call site below is a real, load-bearing
event a log aggregator can already alert/dashboard on by event name;
a future metrics backend can be layered on top of these exact call
sites without any of them changing shape.

Safety-critical constraint, structural rather than merely documented:
every function's parameter list is a closed, explicit set of
identifiers/enums/durations/counts. None of them accept a free-text or
PII-shaped field -- there is no code path by which image bytes, a
base64 payload, a JWT/refresh token, or any user constraint value
(allergies, avoid_ingredients, pregnancy/nursing status) could reach
a log line through this module.

user_id/request_id/analysis_id/job_id are logged as structured *log*
fields (correlation data for tracing one request through the
pipeline), never as metric labels -- there is no metrics client here
for that distinction to apply to yet, but the same discipline holds if
one is added later: a high-cardinality identifier belongs in a trace/
log, never in a label set a time-series database has to index.
"""
import logging
from typing import Optional

logger = logging.getLogger("app.observability")


def _emit(event: str, **fields) -> None:
    logger.info(event, extra={"event": event, **fields})


def analysis_submission(*, request_id: str, user_id: str, outcome: str) -> None:
    """outcome: QUEUED | REPLAY | QUOTA_DENIED | CONSENT_REQUIRED |
    INVALID_IMAGE | STORAGE_DISABLED | ERROR"""
    _emit("analysis_submission", request_id=request_id, user_id=user_id, outcome=outcome)


def queue_claim(*, job_type: str, job_id: Optional[str], claimed: bool) -> None:
    _emit("queue_claim", job_type=job_type, job_id=job_id, claimed=claimed)


def queue_wait_seconds(*, job_id: str, wait_seconds: float) -> None:
    _emit("queue_wait", job_id=job_id, wait_seconds=round(wait_seconds, 3))


def processing_result(
    *, job_id: str, analysis_id: str, outcome: str, duration_seconds: float,
    error_code: Optional[str] = None,
) -> None:
    """outcome: SUCCESS | RETRY | DEAD_LETTER. error_code, when
    present, is always one of the closed set of safe classifications
    app/workers/analysis_worker.py's classify_failure() assigns --
    never an exception message or stack trace."""
    _emit(
        "analysis_processing", job_id=job_id, analysis_id=analysis_id, outcome=outcome,
        duration_seconds=round(duration_seconds, 3), error_code=error_code,
    )


def image_cleanup_failure(*, analysis_request_id: str) -> None:
    _emit("image_cleanup_failure", analysis_request_id=analysis_request_id)


def capture_quality(*, analysis_id: Optional[str], status: str) -> None:
    """status: PASS | BORDERLINE | FAIL"""
    _emit("capture_quality", analysis_id=analysis_id, status=status)


def metric_abstention(*, analysis_id: Optional[str], metric_name: str) -> None:
    _emit("metric_abstention", analysis_id=analysis_id, metric_name=metric_name)


def no_compatible_product(*, analysis_id: Optional[str], category: str) -> None:
    _emit("no_compatible_product", analysis_id=analysis_id, category=category)


def quota_denial(*, user_id: str) -> None:
    _emit("quota_denial", user_id=user_id)


def rate_limit_denial(*, policy: str, identity_kind: str) -> None:
    """identity_kind: "ip" | "user" -- the *kind* of identity that was
    limited, never the actual IP/user_id value, so this one call site
    stays honestly non-identifying even though most events above do
    carry a real user_id/request_id/analysis_id (correlation data, not
    a metric label -- see module docstring)."""
    _emit("rate_limit_denial", policy=policy, identity_kind=identity_kind)
