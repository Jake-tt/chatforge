"""User memory graph: what the assistant remembers about a member across conversations.

Entities (the member, people, places, things) joined by facts: subject -predicate-> object
entity, or a plain value. Every fact records the conversations it came from, so deleting a
conversation forgets what came only from it (a fact confirmed elsewhere stays).

Members only: a guest is anonymous and short-lived, so its conversations keep their own
summaries and nothing more. `add_fact` is the one write path, used by the agent's
`remember` tool and the extraction task.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import MemoryEntity, MemoryFact, MemoryFactSource, User

MAX_NAME_CHARS = 200
MAX_PREDICATE_CHARS = 100
MAX_VALUE_CHARS = 500


def normalize(name: str) -> str:
    """The key an entity is matched by: case and spacing don't make a new entity."""
    return " ".join(name.split()).casefold()[:MAX_NAME_CHARS]


@dataclass(frozen=True)
class FactView:
    id: uuid.UUID
    subject: str
    predicate: str
    object: str
    updated_at: datetime


async def _entity(session: AsyncSession, user_id: uuid.UUID, name: str) -> MemoryEntity:
    name = " ".join(name.split())[:MAX_NAME_CHARS]
    key = normalize(name)
    found = await session.scalar(
        select(MemoryEntity).where(MemoryEntity.user_id == user_id, MemoryEntity.key == key))
    if found is None:
        found = MemoryEntity(user_id=user_id, name=name, key=key)
        session.add(found)
        await session.flush()
    return found


async def add_fact(session: AsyncSession, user_id: uuid.UUID, subject: str, predicate: str, *,
                   obj: str | None = None, value: str | None = None,
                   conversation_id: uuid.UUID) -> MemoryFact:
    """Store a fact, or add this conversation as one more source of the same fact."""
    predicate = " ".join(predicate.split())[:MAX_PREDICATE_CHARS]
    subject_entity = await _entity(session, user_id, subject)
    object_entity = await _entity(session, user_id, obj) if obj else None
    value = " ".join(value.split())[:MAX_VALUE_CHARS] if value else None

    fact = await session.scalar(select(MemoryFact).where(
        MemoryFact.user_id == user_id, MemoryFact.subject_id == subject_entity.id,
        MemoryFact.predicate == predicate,
        MemoryFact.object_id == object_entity.id if object_entity else
        MemoryFact.object_id.is_(None),
        MemoryFact.value == value if value is not None else MemoryFact.value.is_(None)))
    if fact is None:
        fact = MemoryFact(user_id=user_id, subject_id=subject_entity.id, predicate=predicate,
                          object_id=object_entity.id if object_entity else None, value=value)
        session.add(fact)
        await session.flush()
    else:
        fact.updated_at = datetime.now(UTC)
    already = await session.scalar(select(MemoryFactSource.fact_id).where(
        MemoryFactSource.fact_id == fact.id,
        MemoryFactSource.conversation_id == conversation_id))
    if already is None:
        session.add(MemoryFactSource(fact_id=fact.id, conversation_id=conversation_id))
    return fact


async def list_facts(session: AsyncSession, user_id: uuid.UUID) -> list[FactView]:
    subject, target = aliased(MemoryEntity), aliased(MemoryEntity)
    rows = await session.execute(
        select(MemoryFact, subject.name, target.name)
        .join(subject, MemoryFact.subject_id == subject.id)
        .outerjoin(target, MemoryFact.object_id == target.id)
        .where(MemoryFact.user_id == user_id)
        .order_by(MemoryFact.updated_at.desc(), MemoryFact.id))
    return [FactView(f.id, subject_name, f.predicate, target_name or f.value or "",
                     f.updated_at)
            for f, subject_name, target_name in rows]


async def delete_fact(session: AsyncSession, user_id: uuid.UUID, fact_id: uuid.UUID) -> bool:
    result = await session.execute(delete(MemoryFact).where(
        MemoryFact.id == fact_id, MemoryFact.user_id == user_id))
    if result.rowcount:
        await _drop_orphans(session, user_id)
    return bool(result.rowcount)


async def delete_all(session: AsyncSession, user_id: uuid.UUID) -> None:
    # Children first: SQLite doesn't enforce the cascades.
    await session.execute(delete(MemoryFactSource).where(MemoryFactSource.fact_id.in_(
        select(MemoryFact.id).where(MemoryFact.user_id == user_id))))
    await session.execute(delete(MemoryFact).where(MemoryFact.user_id == user_id))
    await session.execute(delete(MemoryEntity).where(MemoryEntity.user_id == user_id))


async def forget_conversations(session: AsyncSession,
                               conversation_ids: list[uuid.UUID]) -> None:
    """Drop these conversations as sources; facts left with no source are forgotten."""
    if not conversation_ids:
        return
    owners = list(await session.scalars(
        select(MemoryFact.user_id).distinct().join(
            MemoryFactSource, MemoryFactSource.fact_id == MemoryFact.id)
        .where(MemoryFactSource.conversation_id.in_(conversation_ids))))
    await session.execute(delete(MemoryFactSource).where(
        MemoryFactSource.conversation_id.in_(conversation_ids)))
    for user_id in owners:
        await session.execute(delete(MemoryFact).where(
            MemoryFact.user_id == user_id,
            ~exists().where(MemoryFactSource.fact_id == MemoryFact.id)))
        await _drop_orphans(session, user_id)


async def _drop_orphans(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Entities no fact mentions any more."""
    await session.execute(delete(MemoryEntity).where(
        MemoryEntity.user_id == user_id,
        ~exists().where(or_(MemoryFact.subject_id == MemoryEntity.id,
                            MemoryFact.object_id == MemoryEntity.id))))


async def is_enabled(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return bool(await session.scalar(select(User.memory_enabled).where(User.id == user_id)))


async def set_enabled(session: AsyncSession, user_id: uuid.UUID, enabled: bool) -> None:
    await session.execute(update(User).where(User.id == user_id)
                          .values(memory_enabled=enabled))
