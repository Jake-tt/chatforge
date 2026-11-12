"""User memory graph: what the assistant remembers about a member across conversations.

Entities (the member, people, places, things) joined by facts: subject -predicate-> object
entity, or a plain value. Every fact records the conversations it came from, so deleting a
conversation forgets what came only from it (a fact confirmed elsewhere stays).

Members only: a guest is anonymous and short-lived, so its conversations keep their own
summaries and nothing more. `add_fact` is the one write path, used by the agent's
`remember` tool and the extraction task.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import MemoryEntity, MemoryFact, MemoryFactSource, User
from app.services.search import BM25Search, Document, SearchHit

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
    object_is_entity: bool = False


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
                     f.updated_at, object_is_entity=target_name is not None)
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


# --- using memory in answers -----------------------------------------------------------

# One plain instruction: "use it when it helps" next to "don't bring it up" read as "don't
# use it" to a local 8B model, which then asked the user for what it had been told.
# Not a numbered source: a local model cited it as [1], pointing at nothing (the same fix
# as for attachments).
CORE_INTRO = ("What you know about this user from earlier conversations. When it's relevant "
              "to the question, use it to tailor your answer; don't list it back. It isn't a "
              "numbered source: don't cite it with [n].")
# How members name themselves in what they ask to be remembered. Shown to the model as
# "사용자": it read "나 → 좋아하는 음료" as being about itself ("제가 좋아하는 음료는…").
_SELF = {"나", "저", "내", "제", "me", "i", "myself", "user", "사용자"}
SHORT_ID = 8  # how much of a fact's id recall shows, and forget accepts

_RRN = re.compile(r"\b\d{6}\s?-\s?[1-8]\d{6}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_ACCOUNT = re.compile(r"\b\d{2,6}-\d{2,6}-\d{2,8}\b")
_PHONE = re.compile(r"^01[016789]-")
_PASSWORD = re.compile(r"(비밀번호|비번|패스워드|password|passwd|\bpw\b)\s*(는|은|:|=|is)?\s*\S+",
                       re.IGNORECASE)


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def sensitive_reason(text: str) -> str | None:
    """Why `text` must not be remembered, if it carries a value that shouldn't be stored.

    Only formatted values: a model is told to leave out sensitive topics, and this catches
    what it lets through anyway or what a user asks to keep.
    """
    if _RRN.search(text):
        return "a resident registration number"
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "a card number"
    for match in _ACCOUNT.finditer(text):
        if len(re.sub(r"\D", "", match.group())) >= 10 and not _PHONE.match(match.group()):
            return "a bank account number"
    if _PASSWORD.search(text):
        return "a password"
    return None


def line(fact: FactView) -> str:
    """The fact as the model reads it (the member's own list keeps their wording)."""
    return f"{_as_model_sees(fact.subject)} → {fact.predicate} → {_as_model_sees(fact.object)}"


def _as_model_sees(name: str) -> str:
    return "사용자" if normalize(name) in _SELF else name


async def core_block(session: AsyncSession, user_id: uuid.UUID, max_chars: int) -> str:
    """The most recently confirmed facts, for the current turn's context ("" if none)."""
    lines: list[str] = []
    used = 0
    for fact in await list_facts(session, user_id):
        entry = f"- {line(fact)}"
        if used + len(entry) > max_chars:
            break
        lines.append(entry)
        used += len(entry) + 1
    if not lines:
        return ""
    return "<memory>\n" + CORE_INTRO + "\n" + "\n".join(lines) + "\n</memory>"


async def recall(session: AsyncSession, user_id: uuid.UUID, query: str,
                 limit: int) -> list[SearchHit]:
    """What the member's memory holds about `query`.

    First by name: entities whose name matches, each with the facts it takes part in (one
    hop). A name pulls in its neighbours, not their other facts. Failing that, facts whose
    text matches ("음료" finds 좋아하는 음료 → 라떼).
    """
    facts = await list_facts(session, user_id)
    edges: dict[str, list[FactView]] = {}
    for fact in facts:
        edges.setdefault(fact.subject, []).append(fact)
        if fact.object_is_entity:
            edges.setdefault(fact.object, []).append(fact)
    names = await BM25Search([Document(n, n, "memory:", n) for n in edges]).search(query, limit)
    if names:
        return [SearchHit(h.doc_id, f"기억 › {h.doc_id}", "memory:",
                          "\n".join(_shown(f) for f in edges[h.doc_id]), h.score)
                for h in names]
    by_fact = [Document(str(f.id), f"기억 › {f.subject}", "memory:", _shown(f)) for f in facts]
    return await BM25Search(by_fact).search(query, limit) if by_fact else []


def _shown(fact: FactView) -> str:
    return f"[{str(fact.id)[:SHORT_ID]}] {line(fact)}"


async def forget_ids(session: AsyncSession, user_id: uuid.UUID, short_ids: list[str]) -> int:
    """Delete the member's facts whose ids start with these (as recall shows them)."""
    wanted = {s.strip().lower() for s in short_ids if len(s.strip()) >= SHORT_ID}
    if not wanted:
        return 0
    ids = [f.id for f in await list_facts(session, user_id)
           if str(f.id)[:SHORT_ID] in {w[:SHORT_ID] for w in wanted}]
    for fact_id in ids:
        await delete_fact(session, user_id, fact_id)
    return len(ids)
