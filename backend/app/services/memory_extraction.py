"""Automatic memory extraction: what a member shared, remembered once the chat goes quiet.

Every reply to a member with memory on schedules `extract_memory` for
`memory_extraction_seconds` later. If that reply is still the conversation's last message
when it runs, the messages after the conversation's extraction cursor are read in one
structured call, and the facts the member stated about themselves are stored through
`memory.add_fact` (a fact already known only gains this conversation as a source).

Rules:
- Members with memory on only, checked again when the task runs: they may have turned
  it off, and a guest has no memory.
- Sensitive topics are kept out three ways: the prompt excludes them; each fact must be
  filed under a category and the sensitive ones are dropped in code (a local 8B model
  extracted health, politics and religion it was told to leave out, but labelled them
  right); and `sensitive_reason` drops any formatted value that still gets through.
- Facts and the cursor move in one transaction guarded by the cursor the run started
  from, so duplicate deliveries store one result.
- A failed, timed-out or unusable extraction stores nothing and leaves the cursor, so
  the next quiet hour reads those messages again.
- The tokens are the service's cost: the member didn't ask for it. They are logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update

from app.models import Conversation, Message
from app.services import memory
from app.services.suggestions import clean_text

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

TASK_NAME = "extract_memory"
MAX_FACTS = 10
MESSAGE_CHARS = 2000  # of each message: facts are short, pasted documents aren't the user

Category = Literal["preference", "relationship", "work", "pet", "plan", "place", "other",
                   "health", "finance", "identification", "secret", "precise_location",
                   "politics", "religion"]
SENSITIVE: frozenset[str] = frozenset(
    ["health", "finance", "identification", "secret", "precise_location", "politics",
     "religion"])
assert SENSITIVE < set(get_args(Category))

EXTRACT_SYSTEM = (
    "You read a chat between a user and an assistant and pick out lasting facts the user "
    "stated about themselves or the people, places and things in their life: preferences, "
    "relationships, work, pets, plans, how they like answers. Use '나' as the subject for "
    "the user. Leave out what only the assistant said, passing small talk, and anything "
    "about the current request only.\n"
    "Never extract these, even if the user said them: health or medical conditions; "
    "financial figures (income, debts, balances, card or account numbers); identification "
    "numbers; passwords or other secrets; precise location (street address, coordinates; "
    "a city is fine); political or religious views.\n"
    "File every fact under its category. Use health, finance, identification, secret, "
    "precise_location, politics or religion for anything of that kind you still list; "
    "place is for a city or region.\n"
    f"Return at most {MAX_FACTS} facts, or an empty list if there is nothing worth keeping. "
    "Write predicates as short noun phrases in the conversation's language, "
    "e.g. 사는 곳, 좋아하는 음료."
)


class ExtractedFact(BaseModel):
    subject: str = Field(description="Who or what it's about; '나' for the user.")
    predicate: str = Field(description="The relation, e.g. 사는 곳, 좋아하는 음료.")
    object: str = Field(description="What it relates to.")
    object_is_name: bool = Field(
        default=False, description="True if `object` names a person, place or thing other "
                                   "facts may refer to; false for a plain value.")
    category: Category = Field(description="What kind of fact this is.")

    @field_validator("category", mode="before")
    @classmethod
    def _known(cls, value: object) -> str:
        # An unlisted label is unknown, not safe: drop the fact rather than the whole run.
        return value if value in get_args(Category) else "secret"

    @field_validator("subject", "predicate", "object", mode="before")
    @classmethod
    def _clean(cls, value: object) -> str:
        return clean_text(value) if isinstance(value, str) else ""


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list,
                                       description=f"At most {MAX_FACTS} facts.")

    @field_validator("facts", mode="after")
    @classmethod
    def _usable(cls, facts: list[ExtractedFact]) -> list[ExtractedFact]:
        return [f for f in facts if f.subject and f.predicate and f.object
                and f.category not in SENSITIVE][:MAX_FACTS]

    @classmethod
    def fake(cls, prompt: str) -> ExtractedFacts:
        return cls()  # the fake provider remembers nothing


def wants_extraction(ctx: AppContext) -> bool:
    return ctx.settings.memory_extraction_seconds > 0


async def schedule(ctx: AppContext, conversation_id: uuid.UUID, after_seq: int) -> None:
    try:
        await ctx.tasks.enqueue(
            TASK_NAME, {"conversation_id": str(conversation_id), "after_seq": after_seq},
            delay_seconds=ctx.settings.memory_extraction_seconds)
    except Exception:
        # The next reply schedules it again; the reply itself is stored and charged.
        log.warning("could not schedule memory extraction", exc_info=True)


async def extract_memory(ctx: AppContext, payload: dict) -> None:
    settings = ctx.settings
    conversation_id = uuid.UUID(payload["conversation_id"])
    async with ctx.sessionmaker() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.deleted_at is not None \
                or conversation.user_id is None:
            return
        if conversation.message_count != payload["after_seq"]:
            return  # the member came back; the reply after this one scheduled its own run
        user_id = conversation.user_id
        if not await memory.is_enabled(session, user_id):
            return
        start = conversation.memory_extracted_through_seq
        messages = list(await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.seq > start)
            .order_by(Message.seq)
            .limit(settings.memory_extraction_batch_messages)))
    if not messages:
        return  # already read by an earlier delivery

    facts: list[ExtractedFact] = []
    usage = None
    if any(m.role == "user" and m.content for m in messages):
        transcript = "\n".join(f"{m.role}: {m.content[:MESSAGE_CHARS]}"
                               for m in messages if m.content)
        try:
            async with asyncio.timeout(settings.memory_extraction_timeout_seconds):
                result, usage = await ctx.llm.structured(EXTRACT_SYSTEM, transcript,
                                                         ExtractedFacts)
        except Exception:
            log.warning("memory extraction failed; will retry later", exc_info=True)
            return
        facts = result.facts

    stored = refused = 0
    async with ctx.sessionmaker() as session:
        moved = await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id,
                   Conversation.memory_extracted_through_seq == start)
            .values(memory_extracted_through_seq=messages[-1].seq))
        if moved.rowcount != 1:
            await session.rollback()
            log.info("memory extraction superseded by a concurrent run")
            return
        if not await memory.is_enabled(session, user_id):
            await session.rollback()  # turned off while the model was reading
            return
        for f in facts:
            if memory.sensitive_reason(f"{f.subject} {f.predicate} {f.object}"):
                refused += 1
                continue
            await memory.add_fact(
                session, user_id, memory.as_stored(f.subject), f.predicate,
                obj=memory.as_stored(f.object) if f.object_is_name else None,
                value=None if f.object_is_name else f.object,
                conversation_id=conversation_id)
            stored += 1
        await session.commit()
    log.info("memory extracted", extra={
        "through_seq": messages[-1].seq, "read_messages": len(messages),
        "stored_facts": stored, "refused_facts": refused,
        # Not charged: the service's cost, logged so it can be watched.
        "input_tokens": usage.input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
    })
