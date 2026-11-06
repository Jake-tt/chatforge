"""Recap: what a user sees when they come back to a conversation after a while.

Written the first time they reopen it after `recap_after_seconds` of quiet, from the
running summary and the latest messages, and stored with the seq it covers so later visits
reuse it until the conversation moves on. A conversation nobody returns to costs nothing.

The user didn't ask for it, so like idle compaction its tokens are the service's cost
(logged, not charged). Any failure means no recap, never a failed page.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.services.suggestions import clean_chips, clean_text

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

RECAP_CHARS = 300
RECAP_QUESTIONS = 3
QUESTION_CHARS = 60
TAIL_MESSAGES = 6
TAIL_CHARS = 500

RECAP_SYSTEM = (
    "The user is coming back to this conversation after a break. In `recap`, remind them in "
    f"two or three sentences (under {RECAP_CHARS} characters) where it left off: what they "
    "wanted, what was settled, and what is still open. Speak to the user directly, in "
    f"their language. In `questions`, give up to {RECAP_QUESTIONS} short questions they "
    "could ask next to pick it up again, phrased as they would type them."
)


class RecapOut(BaseModel):
    recap: str = Field(description="Two or three sentences on where the conversation left off.")
    questions: list[str] = Field(default_factory=list,
                                 description="Short questions to pick the conversation up.")

    @field_validator("recap", mode="before")
    @classmethod
    def _clean_recap(cls, value: object) -> str:
        return clean_text(value)[:RECAP_CHARS] if isinstance(value, str) else ""

    @field_validator("questions", mode="before")
    @classmethod
    def _clean_questions(cls, value: object) -> list[str]:
        return clean_chips(value, limit=RECAP_QUESTIONS, max_chars=QUESTION_CHARS)

    @classmethod
    def fake(cls, prompt: str) -> RecapOut:
        return cls(recap="지난 대화에서 이야기하던 내용을 이어서 볼까요?",
                   questions=["이어서 알려줘"])


class Recap(BaseModel):
    recap: str
    questions: list[str]
    through_seq: int


async def recap_for(ctx: AppContext, session: AsyncSession,
                    conversation: Conversation) -> Recap | None:
    settings = ctx.settings
    through = conversation.message_count
    if through < settings.recap_min_messages:
        return None  # short enough to read
    if conversation.recap and conversation.recap_through_seq == through:
        return Recap(recap=conversation.recap, questions=conversation.recap_questions or [],
                     through_seq=through)
    tail = list(await session.scalars(
        select(Message).where(Message.conversation_id == conversation.id,
                              Message.seq > through - TAIL_MESSAGES)
        .order_by(Message.seq)))
    if not tail or _seconds_since(tail[-1].created_at) < settings.recap_after_seconds:
        return None  # they never left

    # End the read transaction: the model call can take seconds, and holding a
    # transaction open that long ties up a pooled connection for nothing.
    await session.commit()

    lines = "\n".join(f"{m.role}: {m.content[:TAIL_CHARS]}" for m in tail)
    prompt = (f"Summary of the earlier messages:\n{conversation.summary or '(none)'}\n\n"
              f"Latest messages:\n{lines}")
    try:
        async with asyncio.timeout(settings.recap_timeout_seconds):
            out, usage = await ctx.llm.structured(RECAP_SYSTEM, prompt, RecapOut)
    except Exception:
        log.warning("recap failed", exc_info=True)
        return None
    # Uncharged: the service's cost, logged so it can be watched.
    log.info("recap written", extra={"conversation_id": str(conversation.id),
                                     "through_seq": through, "input_tokens": usage.input_tokens,
                                     "output_tokens": usage.output_tokens})
    if not out.recap:
        return None
    # Only forward: a concurrent visit may have written one already, for this seq or later.
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation.id, Conversation.recap_through_seq < through)
        .values(recap=out.recap, recap_questions=out.questions, recap_through_seq=through))
    await session.commit()
    return Recap(recap=out.recap, questions=out.questions, through_seq=through)


def _seconds_since(at: datetime) -> float:
    if at.tzinfo is None:  # SQLite returns naive datetimes; they are stored in UTC
        at = at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - at).total_seconds()
