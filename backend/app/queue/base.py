"""Provider-neutral job queue contract (Phase 15 of the
platform-foundation brief). Mirrors the shape `app/storage/base.py`
established for object storage: a plain ABC business logic depends on,
with exactly one concrete implementation for now
(`PostgresJobQueue`) -- a future Redis- or SQS-backed queue would
implement this same interface without any caller changing.

Deliberately does not commit to any particular queue product. Per this
phase's own brief: "Do NOT prematurely deploy Kafka/Pulsar merely
because future scale may require them." A Postgres-table-backed queue
is the launch implementation precisely because Postgres is already the
one thing every deployment of this application has (`PRODUCTION_ARCHITECTURE.md`
principle 1) -- introducing a second durable store for this alone
would be exactly the complexity tax `SCALING_TRIGGERS.md` argues
against paying before it's measured as necessary.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg


class JobQueueError(Exception):
    """Base class for every error this abstraction raises."""


class JobNotFoundError(JobQueueError):
    """Raised by `acknowledge`/`fail` when the job id doesn't exist,
    or exists but isn't currently claimed by anyone (so acknowledging
    or failing it would be acting on a job this caller never held)."""


@dataclass(frozen=True)
class Job:
    id: UUID
    job_type: str
    payload: Dict[str, Any]
    request_id: Optional[str]
    status: str
    created_at: datetime


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(
        self,
        job_type: str,
        payload: Dict[str, Any],
        *,
        request_id: Optional[str] = None,
        conn: Optional[asyncpg.Connection] = None,
    ) -> Job:
        """Creates a new pending job. When `request_id` is given and a
        job of the same `job_type` with that exact `request_id` already
        exists (regardless of its current status), returns *that*
        existing job unchanged rather than creating a duplicate -- this
        is the idempotency guarantee Phase 16 asks for: the same
        logical request, submitted twice, produces one logical job.

        `conn`, when given, is used directly instead of acquiring a new
        connection from the pool -- lets a caller (Part V, Phase 24's
        AnalysisSubmissionService) run this INSERT inside the same
        transaction as marking the corresponding analysis_requests row
        QUEUED, so both commit or neither does."""

    @abstractmethod
    async def claim(self, job_type: str, *, visibility_timeout_seconds: int = 300) -> Optional[Job]:
        """Atomically claims one pending-and-ready (next_attempt_at, if
        set, must have passed) or previously-claimed-but-expired job of
        the given type, or returns None if none are available. Two
        concurrent callers claiming from the same `job_type` must never
        receive the same job -- this is the core exclusivity guarantee
        a job queue exists to provide."""

    @abstractmethod
    async def acknowledge(self, job_id: UUID) -> None:
        """Marks a claimed job completed. Raises JobNotFoundError if
        the job doesn't exist or isn't currently claimed."""

    @abstractmethod
    async def fail(self, job_id: UUID, error: str, *, retryable: bool = False) -> None:
        """Marks a claimed job failed. Raises JobNotFoundError if the
        job doesn't exist or isn't currently claimed.

        retryable=False (default, and the only behavior that existed
        before Part VI, Phase 27): the job moves straight to the
        terminal 'failed' state -- a real dead-letter, no more
        claiming. Matches this abstraction's original contract exactly
        for any existing caller that doesn't pass this argument.

        retryable=True: if attempt_count is still below max_attempts,
        the job goes back to 'pending' with an exponentially-backed-off
        next_attempt_at and an incremented attempt_count, so a future
        claim() can pick it up again -- once attempts are exhausted, it
        still lands in 'failed', same terminal state either way (Phase
        29's "job -> dead/failed terminal state")."""

    @abstractmethod
    async def extend_visibility(self, job_id: UUID, additional_seconds: int) -> None:
        """Heartbeat (Part VI, Phase 28): pushes claimed_until further
        into the future for a job this caller still holds and is still
        legitimately processing, so another worker doesn't reclaim it
        out from under a CV run that's simply taking longer than the
        original visibility_timeout_seconds. Raises JobNotFoundError if
        the job doesn't exist or isn't currently claimed by anyone."""
