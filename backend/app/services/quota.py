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
  Redis access goes through `RedisGuard`, whose circuit breaker keeps an outage cheap.
"""

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageDaily
from app.services.redis_guard import RedisGuard

USAGE_CACHE_TTL_SECONDS = 300


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
    def __init__(self, guard: RedisGuard, timezone: str):
        self._guard = guard
        self._tz = ZoneInfo(timezone)

    def today(self) -> date:
        return datetime.now(self._tz).date()

    def _resets_at(self, day: date) -> datetime:
        return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=self._tz)

    @staticmethod
    def _usage_key(subject: str, day: date) -> str:
        return f"usage:{subject}:{day.isoformat()}"

    async def status(self, session: AsyncSession, subject: str, limit: int) -> QuotaStatus:
        day = self.today()
        return QuotaStatus(await self._used(session, subject, day), limit, self._resets_at(day))

    async def _used(self, session: AsyncSession, subject: str, day: date) -> int:
        key = self._usage_key(subject, day)
        cached = await self._guard.call(lambda r: r.get(key), None)
        if cached is not None:
            return int(cached)
        used = await self._used_from_db(session, subject, day)
        await self._guard.call(lambda r: r.set(key, used, ex=USAGE_CACHE_TTL_SECONDS), None)
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
        await self._guard.call(lambda r: r.delete(*keys), None)

    async def hit_rate_limit(self, bucket: str, limit: int, window_seconds: int) -> bool:
        """Fixed-window counter. Returns True when the caller is over the limit."""
        key = f"rl:{bucket}:{int(time.time() // window_seconds)}"

        async def incr(r: Redis) -> int:
            async with r.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds)
                count, _ = await pipe.execute()
            return count

        count = await self._guard.call(incr, 0)  # fail open
        return count > limit
