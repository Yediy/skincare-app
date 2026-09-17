"""Bounded minimum-response-duration timing normalization
(independent-review timing-enumeration fix on top of the V1 account
recovery pass).

POST /password/forgot's response-body enumeration safety
(PasswordResetService.request_reset returning identically regardless
of account eligibility) is necessary but not sufficient: before this
fix, only an eligible account's request paid the cost of an outbound
Resend HTTP call, an unbounded and comparatively enormous latency
signal an attacker could distinguish statistically across many
requests. Moving email delivery to a durable out-of-band job (see
app/workers/password_reset_email_worker.py) removes that dominant
signal from the request path entirely. What remains is a much smaller,
bounded residual: an eligible request does a little more local
Postgres work (issuing a token, enqueueing a job) than an ineligible
one (a single lookup). `TimingNormalizer` pads every request out to a
randomly chosen target duration so that residual difference is masked
by jitter that has nothing to do with which branch actually ran.

This is NOT a claim of true constant-time HTTP behavior -- TLS
handshake reuse, OS scheduling, GC pauses, and network jitter are all
real, unremovable variance this can't and doesn't attempt to erase.
It bounds the one signal actually within this application's control
(its own processing time) to a small, randomized range chosen
independently of the very thing an attacker is trying to learn.
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


@dataclass
class TimingNormalizer:
    """`monotonic`/`random_fn`/`sleep_fn` are constructor-injectable
    (not hardcoded calls to `time.monotonic`/`random.uniform`/
    `asyncio.sleep`) specifically so tests can prove the ordering and
    arithmetic in `run()` deterministically -- see
    tests/domain/test_timing_normalization.py -- without a single
    real sleep or wall-clock assertion anywhere in this suite."""

    min_seconds: float
    max_seconds: float
    monotonic: Callable[[], float] = field(default=time.monotonic)
    random_fn: Callable[[float, float], float] = field(default=random.uniform)
    sleep_fn: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    def __post_init__(self) -> None:
        if self.min_seconds < 0 or self.max_seconds < self.min_seconds:
            raise ValueError("TimingNormalizer requires 0 <= min_seconds <= max_seconds")

    def choose_target_seconds(self) -> float:
        return self.random_fn(self.min_seconds, self.max_seconds)

    async def run(self, work: Callable[[], Awaitable[T]]) -> T:
        """Selects the target duration BEFORE calling `work` -- the
        single structural fact that makes the target genuinely
        independent of whatever `work` is about to discover (e.g.
        whether an account is eligible). `work` itself decides which
        branch to execute; this function never inspects or is passed
        `work`'s result to influence timing, only to return it to the
        caller unchanged."""
        target_seconds = self.choose_target_seconds()
        start = self.monotonic()
        result = await work()
        elapsed = self.monotonic() - start
        remaining = target_seconds - elapsed
        if remaining > 0:
            await self.sleep_fn(remaining)
        return result
