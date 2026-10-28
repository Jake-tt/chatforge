"""The circuit breaker itself: when it opens, how it probes, when it closes.

These drive `RedisGuard` directly because its state machine is the subject; the
HTTP-level effects of an outage are in test_resilience.py.
"""

import asyncio

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import MaxConnectionsError, ResponseError

from app.services.redis_guard import RedisGuard


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FlakyRedis:
    """Fails with `error` while `down` is set; records how many calls reached it."""

    def __init__(self, error: Exception | None = None):
        self.down = False
        self.calls = 0
        self.error = error or RedisConnectionError("redis is down")

    async def get(self, key):
        self.calls += 1
        if self.down:
            raise self.error
        return "value"


def _guard(redis, clock, threshold=3, cooldown=10.0) -> RedisGuard:
    return RedisGuard(redis, failure_threshold=threshold, cooldown_seconds=cooldown,
                      clock=clock)


async def _get(guard: RedisGuard):
    return await guard.call(lambda r: r.get("k"), "default")


async def test_a_single_blip_does_not_open_the_breaker():
    # One dropped packet must not switch off rate limits for the whole cooldown.
    redis, clock = FlakyRedis(), Clock()
    guard = _guard(redis, clock)
    redis.down = True
    assert await _get(guard) == "default"
    redis.down = False
    assert await _get(guard) == "value"
    assert redis.calls == 2


async def test_consecutive_failures_open_the_breaker():
    redis, clock = FlakyRedis(), Clock()
    guard = _guard(redis, clock, threshold=3)
    redis.down = True
    for _ in range(5):
        assert await _get(guard) == "default"
    assert redis.calls == 3  # the rest were short-circuited


@pytest.mark.parametrize("error", [ResponseError("WRONGTYPE"), MaxConnectionsError("pool")])
async def test_errors_that_say_nothing_about_reachability_do_not_open_the_breaker(error):
    # A bad command or our own exhausted pool would otherwise switch off rate limits for
    # every user. They still fall back to the default.
    redis, clock = FlakyRedis(error), Clock()
    guard = _guard(redis, clock, threshold=1)
    redis.down = True
    assert [await _get(guard) for _ in range(3)] == ["default"] * 3
    assert redis.calls == 3


async def test_a_success_resets_the_failure_count():
    redis, clock = FlakyRedis(), Clock()
    guard = _guard(redis, clock, threshold=3)
    for down in (True, True, False, True, True):
        redis.down = down
        await _get(guard)
    redis.down = False
    assert await _get(guard) == "value"  # still closed: never 3 failures in a row
    assert redis.calls == 6


async def test_after_the_cooldown_one_probe_closes_the_breaker():
    redis, clock = FlakyRedis(), Clock()
    guard = _guard(redis, clock, threshold=1, cooldown=10.0)
    redis.down = True
    await _get(guard)
    redis.down = False
    clock.now += 9.9
    assert await _get(guard) == "default"
    clock.now += 0.2
    assert await _get(guard) == "value"
    assert await _get(guard) == "value"
    assert redis.calls == 3


async def test_a_failed_probe_reopens_for_a_full_cooldown():
    # Half-open: one failure is enough evidence; it doesn't need the threshold again.
    redis, clock = FlakyRedis(), Clock()
    guard = _guard(redis, clock, threshold=3, cooldown=10.0)
    redis.down = True
    for _ in range(3):
        await _get(guard)
    clock.now += 10.1
    assert await _get(guard) == "default"  # the probe
    clock.now += 9.9
    assert await _get(guard) == "default"
    assert redis.calls == 4


class GatedRedis:
    """Holds every call until `release` is set, then answers (or fails with `error`)."""

    def __init__(self, error: Exception | None = None):
        self.release = asyncio.Event()
        self.calls = 0
        self.error = error

    async def get(self, key):
        self.calls += 1
        await self.release.wait()
        if self.error:
            raise self.error
        return "value"


async def _open(guard: RedisGuard, clock: Clock) -> None:
    """Open a threshold-1 breaker and let its cooldown run out."""
    down = FlakyRedis()
    down.down = True
    await guard.call(lambda r: down.get("k"), "default")
    clock.now += 10.1


async def test_failures_that_land_after_the_breaker_opened_do_not_extend_it():
    # Calls in flight when Redis went away fail late; they must not push the probe back.
    redis, clock = GatedRedis(RedisConnectionError("down")), Clock()
    guard = _guard(redis, clock, threshold=1, cooldown=10.0)
    async with asyncio.timeout(1):
        first, late = asyncio.create_task(_get(guard)), asyncio.create_task(_get(guard))
        await asyncio.sleep(0)
        redis.release.set()
        await first  # opens the breaker at t0
        clock.now += 5
        await late
    clock.now += 5.1
    redis.error = None
    assert await _get(guard) == "value"


async def test_only_one_request_probes_while_half_open():
    # After a cooldown, every waiting request hitting a still-sick Redis at once would
    # cost each of them a timeout. Others get the default until the probe answers.
    redis, clock = GatedRedis(), Clock()
    guard = _guard(redis, clock, threshold=1)
    await _open(guard, clock)

    async with asyncio.timeout(1):  # a second probe would wait on the gate forever
        probe = asyncio.create_task(_get(guard))
        await asyncio.sleep(0)
        others = [await _get(guard) for _ in range(3)]
        redis.release.set()
        assert (await probe, others, redis.calls) == ("value", ["default"] * 3, 1)
        assert await _get(guard) == "value"


async def test_a_cancelled_probe_lets_the_next_request_probe():
    redis, clock = GatedRedis(), Clock()
    guard = _guard(redis, clock, threshold=1)
    await _open(guard, clock)

    async with asyncio.timeout(1):
        probe = asyncio.create_task(_get(guard))
        await asyncio.sleep(0)
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)
        redis.release.set()
        assert await _get(guard) == "value"
