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
    async def enqueue(self, job_type: str, payload: Dict[str, Any], *, request_id: Optional[str] = None) -> Job:
        """Creates a new pending job. When `request_id` is given and a
        job of the same `job_type` with that exact `request_id` already
        exists (regardless of its current status), returns *that*
        existing job unchanged rather than creating a duplicate -- this
        is the idempotency guarantee Phase 16 asks for: the same
        logical request, submitted twice, produces one logical job."""

    @abstractmethod
    async def claim(self, job_type: str, *, visibility_timeout_seconds: int = 300) -> Optional[Job]:
        """Atomically claims one pending (or previously-claimed but
        expired) job of the given type, or returns None if none are
        available. Two concurrent callers claiming from the same
        `job_type` must never receive the same job -- this is the
        core exclusivity guarantee a job queue exists to provide."""

    @abstractmethod
    async def acknowledge(self, job_id: UUID) -> None:
        """Marks a claimed job completed. Raises JobNotFoundError if
        the job doesn't exist or isn't currently claimed."""

    @abstractmethod
    async def fail(self, job_id: UUID, error: str) -> None:
        """Marks a claimed job failed, recording `error`. Raises
        JobNotFoundError if the job doesn't exist or isn't currently
        claimed. Does not itself retry -- a failed job's payload/
        request_id are preserved, so a caller can decide to re-enqueue
        with the same request_id (which, per `enqueue`'s contract,
        would return this same now-failed job rather than a fresh
        attempt -- retry-after-failure policy is deliberately left to
        the caller, not decided by this abstraction)."""
