"""app.domain.timing_normalization.TimingNormalizer -- the independent-
review timing-enumeration fix. Every test here injects a fake
monotonic/random_fn/sleep_fn: no real sleep, no wall-clock assertion,
no flakiness -- see the module's own docstring for why real timing
behavior can't be (and isn't claimed to be) proven this way.
"""
import pytest

from app.domain.timing_normalization import TimingNormalizer


def test_rejects_max_less_than_min():
    with pytest.raises(ValueError):
        TimingNormalizer(min_seconds=0.5, max_seconds=0.1)


def test_rejects_negative_min():
    with pytest.raises(ValueError):
        TimingNormalizer(min_seconds=-0.1, max_seconds=0.5)


async def test_sleeps_for_exactly_the_remaining_duration():
    """target=0.3s, work() takes 0.1s of monotonic time (per the fake
    clock) -> sleep_fn must be called with exactly 0.2s remaining."""
    monotonic_values = iter([100.0, 100.1])  # start, then after work()
    sleep_calls = []

    normalizer = TimingNormalizer(
        min_seconds=0.3, max_seconds=0.3,  # deterministic target: random_fn is never even consulted for its range width here
        monotonic=lambda: next(monotonic_values),
        random_fn=lambda lo, hi: 0.3,
        sleep_fn=lambda seconds: sleep_calls.append(seconds) or _immediate_future(),
    )

    async def work():
        return "result"

    result = await normalizer.run(work)
    assert result == "result"
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.2, abs=1e-9)


async def test_does_not_sleep_when_work_already_exceeded_target():
    monotonic_values = iter([100.0, 100.5])  # work() took 0.5s, target is 0.3s
    sleep_calls = []

    normalizer = TimingNormalizer(
        min_seconds=0.3, max_seconds=0.3,
        monotonic=lambda: next(monotonic_values),
        random_fn=lambda lo, hi: 0.3,
        sleep_fn=lambda seconds: sleep_calls.append(seconds) or _immediate_future(),
    )

    async def work():
        return None

    await normalizer.run(work)
    assert sleep_calls == []


async def test_target_is_chosen_before_work_runs_and_independently_of_it():
    """The core enumeration-safety property: the target-duration
    selection (random_fn) must happen before `work` is ever invoked,
    and `work`'s own branch (eligible vs ineligible) must have no way
    to influence which target was chosen -- proven here by recording
    call order and by using two different `work` closures that each
    return a different, distinguishing result, while random_fn always
    receives the exact same (min, max) arguments regardless of which
    one runs."""
    call_order = []
    random_calls = []

    def fake_random(lo, hi):
        random_calls.append((lo, hi))
        call_order.append("target_chosen")
        return 0.25

    normalizer = TimingNormalizer(
        min_seconds=0.1, max_seconds=0.4,
        monotonic=lambda: 0.0,
        random_fn=fake_random,
        sleep_fn=lambda seconds: _immediate_future(),
    )

    async def eligible_work():
        call_order.append("eligible_work_ran")
        return "eligible"

    async def ineligible_work():
        call_order.append("ineligible_work_ran")
        return "ineligible"

    call_order.clear()
    result_a = await normalizer.run(eligible_work)
    order_a = list(call_order)

    call_order.clear()
    result_b = await normalizer.run(ineligible_work)
    order_b = list(call_order)

    assert result_a == "eligible"
    assert result_b == "ineligible"
    # Target selection strictly precedes the work closure's own body in
    # both cases.
    assert order_a == ["target_chosen", "eligible_work_ran"]
    assert order_b == ["target_chosen", "ineligible_work_ran"]
    # random_fn was called with the identical (min, max) range both
    # times -- nothing about which branch was about to run reached it.
    assert random_calls == [(0.1, 0.4), (0.1, 0.4)]


async def test_choose_target_seconds_delegates_to_random_fn():
    normalizer = TimingNormalizer(min_seconds=0.2, max_seconds=0.5, random_fn=lambda lo, hi: 0.35)
    assert normalizer.choose_target_seconds() == 0.35


def _immediate_future():
    import asyncio

    async def _noop():
        return None

    return _noop()
