"""Daily token quotas and per-minute rate limits.

Design:
- `usage_daily` in Postgres is the source of truth; it is updated in the same transaction
  that stores the assistant message, so usage can never drift from stored messages.
- Redis caches the day's total (cache-aside). Writes invalidate the key after commit instead
  of incrementing it. A reader that loaded the old DB value just before the commit can still
  re-populate a stale value after the invalidation, so the TTL is short: it bounds that
  staleness to a few minutes, which is acceptable for a soft daily quota.
- If Redis is down, quota checks fall back to the database (still enforced), while the
  per-minute rate limiter fails open (availability over strictness for a soft limit).
  A small circuit breaker skips Redis for a few seconds after a failure, so an outage costs
  one connect timeout per breaker window instead of one per request.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageDaily

log = logging.getLogger(__name__)

USAGE_CACHE_TTL_SECONDS = 300
REDIS_COOLDOWN_SECONDS = 10.0

T = TypeVar("T")


@dataclass(frozen=True)
class QuotaStatus:
    used: int
    limit: int
    resets_at: datetime

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exceeded(self) -> bool:
        return self.used >= self.limit


class QuotaService:
    def __init__(self, redis: Redis, timezone: str):
        self._redis = redis
        self._tz = ZoneInfo(timezone)
        self._redis_down_until = 0.0

    def today(self) -> date:
        return datetime.now(self._tz).date()

    def _resets_at(self, day: date) -> datetime:
        return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=self._tz)

    @staticmethod
    def _usage_key(subject: str, day: date) -> str:
        return f"usage:{subject}:{day.isoformat()}"

    async def _redis_call(self, op: Callable[[Redis], Awaitable[T]], default: T) -> T:
        """Run a Redis operation; on failure (or while the breaker is open) return `default`."""
        if time.monotonic() < self._redis_down_until:
            return default
        try:
            return await op(self._redis)
        except RedisError as exc:
            self._redis_down_until = time.monotonic() + REDIS_COOLDOWN_SECONDS
            log.warning("redis unavailable, bypassing for %ss: %s", REDIS_COOLDOWN_SECONDS, exc)
            return default

    async def status(self, session: AsyncSession, subject: str, limit: int) -> QuotaStatus:
        day = self.today()
        return QuotaStatus(await self._used(session, subject, day), limit, self._resets_at(day))

    async def _used(self, session: AsyncSession, subject: str, day: date) -> int:
        key = self._usage_key(subject, day)
        cached = await self._redis_call(lambda r: r.get(key), None)
        if cached is not None:
            return int(cached)
        used = await self._used_from_db(session, subject, day)
        await self._redis_call(lambda r: r.set(key, used, ex=USAGE_CACHE_TTL_SECONDS), None)
        return used

    @staticmethod
    async def _used_from_db(session: AsyncSession, subject: str, day: date) -> int:
        tokens = await session.scalar(
            select(UsageDaily.tokens).where(UsageDaily.subject == subject, UsageDaily.day == day)
        )
        return tokens or 0

    async def add_usage(self, session: AsyncSession, subject: str, tokens: int,
                        day: date | None = None) -> None:
        """Add tokens inside the caller's transaction. Call `invalidate` after commit."""
        if tokens <= 0:
            return
        day = day or self.today()
        dialect = session.get_bind().dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        stmt = insert(UsageDaily).values(subject=subject, day=day, tokens=tokens)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UsageDaily.subject, UsageDaily.day],
            set_={"tokens": UsageDaily.tokens + stmt.excluded.tokens},
        )
        await session.execute(stmt)

    async def invalidate(self, *subjects: str, day: date | None = None) -> None:
        day = day or self.today()
        keys = [self._usage_key(s, day) for s in subjects]
        # If this fails, the stale value expires with the short TTL; the DB stays correct.
        await self._redis_call(lambda r: r.delete(*keys), None)

    async def hit_rate_limit(self, bucket: str, limit: int, window_seconds: int) -> bool:
        """Fixed-window counter. Returns True when the caller is over the limit."""
        key = f"rl:{bucket}:{int(time.time() // window_seconds)}"

        async def incr(r: Redis) -> int:
            async with r.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds)
                count, _ = await pipe.execute()
            return count

        count = await self._redis_call(incr, 0)  # fail open
        return count > limit
