"""Recent-history cache for building LLM context.

Each chat request needs the conversation's last N messages. They are cached in a Redis list
(`history:{owner}:{conversation}`) and appended to as messages are stored, so a request
usually reads its context without touching Postgres.

Correctness rules — the cache may be missing or stale, never silently wrong:
- Every cached entry carries the message's `seq`. A read is a hit only if the seqs are
  contiguous and end at the conversation's current `message_count` (which the caller has
  just read/incremented). A list missing a message — e.g. a concurrent request re-filled
  the key from a DB read taken just before another message committed — fails that check
  and is rebuilt from the database instead of feeding the model a gap in the conversation.
- Appends use RPUSHX, which only appends to an existing list. A missing key stays missing
  until the next read rebuilds it whole, so we never create a list holding only the tail.
- If Redis is down or the key expired, the database is the fallback (via RedisGuard).
"""

import json
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message
from app.services.llm import ChatTurn
from app.services.redis_guard import RedisGuard

HISTORY_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class HistoryEntry:
    seq: int
    role: str
    content: str

    def to_turn(self) -> ChatTurn:
        return ChatTurn(self.role, self.content)


def _encode(entry: HistoryEntry) -> str:
    return json.dumps({"s": entry.seq, "r": entry.role, "c": entry.content}, ensure_ascii=False)


def _decode(raw: str) -> HistoryEntry:
    d = json.loads(raw)
    return HistoryEntry(d["s"], d["r"], d["c"])


class HistoryCache:
    def __init__(self, guard: RedisGuard, limit: int):
        self._guard = guard
        self._limit = limit

    @staticmethod
    def key(owner: str, conversation_id: uuid.UUID) -> str:
        return f"history:{owner}:{conversation_id}"

    async def recent(
        self, session: AsyncSession, owner: str, conversation_id: uuid.UUID, latest_seq: int
    ) -> list[HistoryEntry]:
        key = self.key(owner, conversation_id)
        raw = await self._guard.call(lambda r: r.lrange(key, 0, -1), [])
        cached = [_decode(x) for x in raw]
        if self._is_complete(cached, latest_seq):
            return cached

        rows = await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq.desc())
            .limit(self._limit)
        )
        entries = [HistoryEntry(m.seq, m.role, m.content) for m in rows][::-1]
        if entries:
            await self._guard.call(lambda r: self._fill(r, key, entries), None)
        return entries

    def _is_complete(self, entries: list[HistoryEntry], latest_seq: int) -> bool:
        if not entries or entries[-1].seq != latest_seq:
            return False
        if any(b.seq != a.seq + 1 for a, b in zip(entries, entries[1:], strict=False)):
            return False
        # A full window, or the whole conversation from its first message.
        return len(entries) >= min(self._limit, latest_seq)

    @staticmethod
    async def _fill(r: Redis, key: str, entries: list[HistoryEntry]) -> None:
        async with r.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.rpush(key, *(_encode(e) for e in entries))
            pipe.expire(key, HISTORY_TTL_SECONDS)
            await pipe.execute()

    async def append(self, owner: str, conversation_id: uuid.UUID, entry: HistoryEntry) -> None:
        key = self.key(owner, conversation_id)

        async def op(r: Redis) -> None:
            async with r.pipeline(transaction=True) as pipe:
                pipe.rpushx(key, _encode(entry))
                pipe.ltrim(key, -self._limit, -1)
                pipe.expire(key, HISTORY_TTL_SECONDS)
                await pipe.execute()

        await self._guard.call(op, None)

    async def invalidate(self, owner: str, conversation_id: uuid.UUID) -> None:
        await self._guard.call(lambda r: r.unlink(self.key(owner, conversation_id)), 0)

    async def invalidate_owner(self, owner: str) -> int:
        """Drop every cached conversation of one owner (bulk delete, guest claim)."""
        return await self._guard.scan_delete(f"history:{owner}:*")
