"""Shared Redis access with a circuit breaker.

Every Redis call in the app goes through `RedisGuard.call`, so all features degrade the
same way when Redis is down: the call returns a caller-chosen default (each feature decides
whether that means "cache miss", "allow" or "skip"), and after one failure Redis is bypassed
for a cooldown window, so an outage costs one connect timeout per window instead of one
per request.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class RedisGuard:
    def __init__(self, redis: Redis, cooldown_seconds: float = 10.0):
        self._redis = redis
        self._cooldown = cooldown_seconds
        self._down_until = 0.0

    async def call[T](self, op: Callable[[Redis], Awaitable[T]], default: T) -> T:
        """Run `op`; on a Redis error (or while the breaker is open) return `default`."""
        if time.monotonic() < self._down_until:
            return default
        try:
            return await op(self._redis)
        except RedisError as exc:
            self._down_until = time.monotonic() + self._cooldown
            log.warning("redis unavailable, bypassing for %ss: %s", self._cooldown, exc)
            return default

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
