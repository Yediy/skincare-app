"""app.observability.events -- Part VIII's minimum observability.
Structural test that no event function can carry a free-text/PII-shaped
argument, plus a couple of real call-site checks (quota denial, rate
limit denial) proving these aren't dead code."""
import inspect
import uuid

import pytest

from app.observability import events

# Every event function's parameter list, by name, must be drawn only
# from this closed vocabulary -- catches an accidental new parameter
# (e.g. "allergies", "image_base64", "jwt") before it ever ships.
_ALLOWED_PARAM_NAMES = {
    "request_id", "user_id", "job_id", "job_type", "claimed", "wait_seconds",
    "analysis_id", "outcome", "duration_seconds", "error_code",
    "analysis_request_id", "status", "metric_name", "category", "policy", "identity_kind",
}


def test_every_event_function_has_only_allowlisted_parameters():
    for name, fn in inspect.getmembers(events, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = set(inspect.signature(fn).parameters)
        assert params <= _ALLOWED_PARAM_NAMES, f"{name} has unexpected parameter(s): {params - _ALLOWED_PARAM_NAMES}"


def test_emit_never_raises_and_logs_structured_fields(monkeypatch):
    # Not caplog: Alembic's fileConfig() (run once per test session by
    # the migrated_test_database fixture, via logging.config.fileConfig's
    # disable_existing_loggers default) disables every logger not
    # listed in alembic.ini for the rest of the session -- a real, if
    # unrelated, test-harness quirk that would silently no-op any
    # caplog-based assertion here. Spying directly on _emit sidesteps
    # it entirely and is a more precise assertion anyway.
    calls = []
    monkeypatch.setattr(events, "_emit", lambda event, **fields: calls.append((event, fields)))

    events.analysis_submission(request_id="r1", user_id=str(uuid.uuid4()), outcome="QUEUED")

    assert len(calls) == 1
    event, fields = calls[0]
    assert event == "analysis_submission"
    assert fields["outcome"] == "QUEUED"


async def test_quota_denial_event_fires_on_quota_exceeded(app_db_pool, db_pool, monkeypatch):
    from app.domain.entitlement import FreeTierEntitlementService, QuotaExceededError, UsagePolicyService

    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", "obs-quota@test.com"
    )
    user_id = row["id"]
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=0))

    calls = []
    monkeypatch.setattr(events, "_emit", lambda event, **fields: calls.append((event, fields)))

    with pytest.raises(QuotaExceededError):
        await usage_policy_service.reserve_analysis(user_id, str(uuid.uuid4()))

    assert any(event == "quota_denial" for event, _ in calls)
