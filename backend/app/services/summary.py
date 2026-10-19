"""Rolling conversation summary.

A long conversation would otherwise either fall off the history window (the model forgets
the start) or slide it by one turn per request, which changes the start of the prompt
every time and defeats prompt caching. Instead, once enough messages pile up after the
summary boundary, a background task folds the older ones into `Conversation.summary` and
moves `summarized_through_seq` forward. Requests send the summary in the system prompt and
only the messages after the boundary, so the prompt prefix is stable between summaries.

Rules:
- The boundary always sits just before a user message, and the last `summary_keep_turns`
  user turns (with everything after them) stay verbatim. An exchange in progress — e.g. the
  assistant asking which plan the user meant — is never folded into the summary, so the
  user's reply still has its question next to it.
- Summary and boundary change in one UPDATE guarded by the boundary the task started from.
  Duplicate or overlapping deliveries (Cloud Tasks is at-least-once) apply one result.
- One run folds at most `summary_batch_messages` of the oldest pending messages, so a long
  backlog (e.g. a conversation older than this feature) catches up over several runs
  instead of one prompt that never fits the timeout.
- The summary is validated (non-empty, about the length limit). A failed or invalid summary
  leaves the conversation as it was: the full window is still sent.
- Tokens are charged to the conversation's owner only for a summary that was applied.
  Retries and lost races are the service's own cost.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.config import Settings
from app.models import Conversation, Message
from app.services.claim import guest_subject, user_subject
from app.services.llm import ChatTurn

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

TASK_NAME = "summarize_conversation"

SUMMARY_SYSTEM = (
    "You maintain the running summary of a chat between a user and an assistant. Merge the "
    "current summary with the new messages into one updated summary of at most {limit} "
    "characters. Keep facts the user shared, their goals and preferences, decisions made, "
    "and questions still open. Leave out greetings and small talk. Write in the language of "
    "the conversation and reply with the summary only."
)

SUMMARY_SECTION = """

<conversation_summary>
Earlier messages of this conversation, summarized:
{summary}
</conversation_summary>"""


def system_prompt(settings: Settings, conversation: Conversation) -> str:
    if not settings.summary_enabled or not conversation.summary:
        return settings.system_prompt
    return settings.system_prompt + SUMMARY_SECTION.format(summary=conversation.summary)


def summarized_through(settings: Settings, conversation: Conversation) -> int:
    """Messages up to this seq are covered by the summary and not sent verbatim."""
    return conversation.summarized_through_seq if settings.summary_enabled else 0


def needs_summary(settings: Settings, pending: list[ChatTurn]) -> bool:
    """`pending`: the messages after the boundary, including the reply just stored."""
    if not settings.summary_enabled:
        return False
    chars = sum(len(t.content) for t in pending)
    return (len(pending) >= settings.summary_trigger_messages
            or chars // 4 >= settings.summary_trigger_tokens)


def _cut(messages: list[Message], keep_turns: int) -> list[Message]:
    """Messages to fold into the summary: all before the `keep_turns`-th last user turn."""
    user_positions = [i for i, m in enumerate(messages) if m.role == "user"]
    if len(user_positions) <= keep_turns:
        return []
    return messages[:user_positions[-keep_turns]]


def _owner_subject(conversation: Conversation) -> str:
    # Read at run time: a guest may have signed up since the task was enqueued.
    if conversation.user_id is not None:
        return user_subject(conversation.user_id)
    return guest_subject(conversation.guest_id)


def _is_usable(summary: str, limit: int) -> bool:
    return bool(summary) and len(summary) <= limit * 1.2


async def summarize_conversation(ctx: AppContext, payload: dict) -> None:
    settings = ctx.settings
    conversation_id = uuid.UUID(payload["conversation_id"])
    async with ctx.sessionmaker() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.deleted_at is not None:
            return
        start = conversation.summarized_through_seq
        previous = conversation.summary
        subject = _owner_subject(conversation)
        messages = list(await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.seq > start)
            .order_by(Message.seq)
            .limit(settings.summary_batch_messages)
        ))
    folded = _cut(messages, settings.summary_keep_turns)
    if not folded:
        return  # already summarized by an earlier delivery

    limit = settings.conversation_summary_char_limit
    transcript = "\n".join(f"{m.role}: {m.content}" for m in folded)
    try:
        async with asyncio.timeout(settings.summary_timeout_seconds):
            result = await ctx.llm.complete(
                SUMMARY_SYSTEM.format(limit=limit),
                f"Current summary:\n{previous or '(none)'}\n\nNew messages:\n{transcript}",
            )
    except Exception:
        log.warning("conversation summary failed; keeping full history", exc_info=True)
        return
    summary = result.text.strip()
    if not _is_usable(summary, limit):
        log.warning("conversation summary rejected", extra={"summary_chars": len(summary)})
        return

    async with ctx.sessionmaker() as session:
        applied = await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id,
                   Conversation.summarized_through_seq == start)
            .values(summary=summary, summarized_through_seq=folded[-1].seq)
        )
        if applied.rowcount != 1:
            await session.rollback()
            log.info("conversation summary superseded by a concurrent run")
            return
        await ctx.quota.add_usage(session, subject, result.usage.billed(settings.token_weights()))
        await session.commit()
    await ctx.quota.invalidate(subject)
    log.info("conversation summarized", extra={
        "through_seq": folded[-1].seq, "folded_messages": len(folded),
        "summary_chars": len(summary),
    })
