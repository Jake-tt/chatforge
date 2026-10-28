"""Shared Redis access with a circuit breaker.

Every Redis call in the app goes through `RedisGuard.call`, so all features degrade the
same way when Redis is down: the call returns a caller-chosen default (each feature decides
whether that means "cache miss", "allow" or "skip").

The breaker has three states:
- closed: calls go to Redis. `failure_threshold` failures in a row open it, so a single
  dropped packet doesn't switch off rate limits for a whole cooldown. Only failures to
  reach Redis count: a bad command or our own exhausted connection pool says nothing
  about whether Redis is up.
- open: calls return the default without touching Redis, so an outage costs nothing per
  request until the cooldown ends.
- half-open: after the cooldown, exactly one call probes Redis while the others keep
  getting the default. Success closes the breaker; failure reopens it for a full
  cooldown. Without this, every request waiting on a still-sick Redis would pay a timeout.

Each call's wait is bounded by the client's socket timeouts (`main.build_redis`); a
breaker can't protect against a Redis that accepts connections and never answers.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import MaxConnectionsError, RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

log = logging.getLogger(__name__)


def _unreachable(exc: RedisError) -> bool:
    return (isinstance(exc, RedisConnectionError | RedisTimeoutError)
            and not isinstance(exc, MaxConnectionsError))


class RedisGuard:
    def __init__(self, redis: Redis, *, failure_threshold: int = 3,
                 cooldown_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self._redis = redis
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._open_until: float | None = None  # None while closed
        self._probing = False

    async def call[T](self, op: Callable[[Redis], Awaitable[T]], default: T) -> T:
        """Run `op`; on a Redis error (or while the breaker is open) return `default`."""
        probe = False
        if self._open_until is not None:
            if self._probing or self._clock() < self._open_until:
                return default
            probe = self._probing = True
        try:
            result = await op(self._redis)
        except RedisError as exc:
            if _unreachable(exc):
                self._failed(exc, probe)
            else:
                log.warning("redis call failed: %s", exc)
            return default
        finally:
            # Also on cancellation, so an abandoned probe doesn't wedge the breaker open.
            if probe:
                self._probing = False
        self._succeeded()
        return result

    def _failed(self, exc: RedisError, probe: bool) -> None:
        if probe:
            self._open_until = self._clock() + self._cooldown
            log.warning("redis still unavailable, bypassing for %ss: %s", self._cooldown, exc)
            return
        if self._open_until is not None:
            return  # a call that started before the breaker opened
        self._failures += 1
        log.debug("redis call failed (%d in a row): %s", self._failures, exc)
        if self._failures >= self._threshold:
            self._open_until = self._clock() + self._cooldown
            log.warning("redis unavailable after %d failures, bypassing for %ss: %s",
                        self._failures, self._cooldown, exc)

    def _succeeded(self) -> None:
        if self._open_until is not None:
            log.info("redis recovered")
        self._failures = 0
        self._open_until = None

    async def scan_delete(self, pattern: str, batch: int = 100) -> int:
        """Delete every key matching `pattern` without blocking Redis.

        `KEYS pattern` walks the whole keyspace in one blocking call, stalling every other
        client while it runs. SCAN walks it in small cursor steps, and UNLINK frees memory
        in the background, so a large delete never monopolises the server.
        """
        async def op(r: Redis) -> int:
            deleted = 0
            chunk: list[str] = []
            async for key in r.scan_iter(match=pattern, count=batch):
                chunk.append(key)
                if len(chunk) >= batch:
                    deleted += await r.unlink(*chunk)
                    chunk.clear()
            if chunk:
                deleted += await r.unlink(*chunk)
            return deleted

        return await self.call(op, 0)
