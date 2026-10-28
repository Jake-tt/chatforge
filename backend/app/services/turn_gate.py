"""Admission for chat turns: one turn per conversation, a few per user.

- Per conversation: a second message while a reply is still streaming would allocate
  `user, user, assistant, assistant`. The turn holds a lock (`SET NX` with a TTL past the
  turn timeout, so a crashed instance can't hold it forever); a concurrent turn gets 409.
- Per user: the daily quota is checked when a turn starts and charged when it ends, so
  parallel turns could each pass the check. Capping concurrent turns bounds that overrun.

Both live in Redis behind `RedisGuard` and fail open when it is down, like the per-minute
rate limit: the guarantees above need Redis; the chat does not.
"""

import secrets
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from app.errors import ApiError
from app.services.redis_guard import RedisGuard


@dataclass(frozen=True)
class Lease:
    lock_key: str
    token: str
    streams_key: str


class TurnGate:
    def __init__(self, guard: RedisGuard, lock_ttl_seconds: int, max_streams: int):
        self._guard = guard
        self._ttl = lock_ttl_seconds
        self._max_streams = max_streams

    async def acquire(self, subject: str, conversation_id: uuid.UUID) -> Lease:
        lease = Lease(f"turn:{conversation_id}", secrets.token_hex(8), f"streams:{subject}")
        locked = await self._guard.call(
            lambda r: r.set(lease.lock_key, lease.token, nx=True, ex=self._ttl), True)
        if not locked:
            raise ApiError(409, "stream_in_progress",
                           "A reply is still being written in this conversation")

        async def count(r: Redis) -> int:
            async with r.pipeline(transaction=True) as pipe:
                pipe.incr(lease.streams_key)
                pipe.expire(lease.streams_key, self._ttl)
                running, _ = await pipe.execute()
            return running

        if await self._guard.call(count, 0) > self._max_streams:
            await self.release(lease)
            raise ApiError(429, "too_many_streams", "Too many replies in progress")
        return lease

    async def release(self, lease: Lease) -> None:
        async def op(r: Redis) -> None:
            # Delete the lock only if it is still ours: after a TTL expiry another turn may
            # hold it. (GET then DEL is not atomic, but only an expired lease could race.)
            if await r.get(lease.lock_key) == lease.token:
                await r.delete(lease.lock_key)
            if await r.decr(lease.streams_key) <= 0:
                await r.delete(lease.streams_key)

        await self._guard.call(op, None)
