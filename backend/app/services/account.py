"""Deleting an account: an immediate step in the request, the purge in the background.

The request does only what must happen at once, in one transaction: it marks the user
deleted, revokes every token (token_version) and frees the email for a new signup. The
user's data can be large, so the purge runs as a task, a batch of conversations per
transaction, so no single transaction holds locks for long.

The purge is idempotent (Cloud Tasks delivers at least once): each step deletes whatever
is left, and a rerun after it finished finds nothing to do. It refuses to touch a user that
isn't marked deleted, so a stale or forged task can't wipe a live account.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.models import Conversation, Guest, Message, UsageDaily, User
from app.services.claim import user_subject

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

TASK_NAME = "purge_user"


def tombstone_email(user_id: uuid.UUID) -> str:
    # Not an address (no "@"), so it can't collide with a signup; unique per user.
    return f"deleted:{user_id}"


async def purge_user(ctx: AppContext, payload: dict) -> None:
    user_id = uuid.UUID(payload["user_id"])
    subject = user_subject(user_id)
    async with ctx.sessionmaker() as session:
        user = await session.get(User, user_id)
    if user is not None and user.deleted_at is None:
        log.warning("refusing to purge a user that is not deleted",
                    extra={"user_id": str(user_id)})
        return

    if user is not None:
        batches = await _delete_conversations(ctx, user_id)
        async with ctx.sessionmaker() as session:
            await session.execute(delete(UsageDaily).where(UsageDaily.subject == subject))
            # Claimed guests are empty shells whose chats moved to this user.
            await session.execute(delete(Guest).where(Guest.claimed_by_user_id == user_id))
            await session.execute(delete(User).where(User.id == user_id,
                                                     User.deleted_at.is_not(None)))
            await session.commit()
        log.info("user purged", extra={"user_id": str(user_id), "batches": batches})
    # Caches last: a request that raced the purge may have re-filled one.
    await ctx.history.invalidate_owner(subject)
    await ctx.quota.invalidate(subject)


async def _delete_conversations(ctx: AppContext, user_id: uuid.UUID) -> int:
    batch, batches = ctx.settings.purge_batch_conversations, 0
    while True:
        async with ctx.sessionmaker() as session:
            ids = list(await session.scalars(
                select(Conversation.id).where(Conversation.user_id == user_id).limit(batch)))
            if not ids:
                return batches
            # Messages explicitly: SQLite doesn't enforce the cascade.
            await session.execute(delete(Message).where(Message.conversation_id.in_(ids)))
            await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
            await session.commit()
        batches += 1
