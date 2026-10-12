"""Guest → member migration.

When a visitor who has been chatting anonymously signs up or logs in, their conversations
and today's token usage move to the member account in one transaction.

Rules:
- Idempotent: claiming the same guest twice for the same user is a no-op.
- A guest claimed by user A is never re-claimed by user B (stale token on a shared device).
- Today's guest usage is merged into the member's usage, so creating a fresh guest and then
  logging in cannot be used to reset the daily quota.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Guest, UsageDaily
from app.services.quota import QuotaService

ClaimStatus = Literal["claimed", "already_claimed", "claimed_by_other", "not_found"]


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    conversations_moved: int = 0
    tokens_merged: int = 0


def guest_subject(guest_id: uuid.UUID) -> str:
    return f"guest:{guest_id}"


def user_subject(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


async def claim_guest(
    session: AsyncSession, quota: QuotaService, guest_id: uuid.UUID, user_id: uuid.UUID
) -> ClaimResult:
    """Must run inside the caller's transaction; caller commits, then invalidates the cache."""
    # Row lock serialises concurrent logins with the same guest token (no-op on SQLite).
    guest = await session.scalar(
        select(Guest).where(Guest.id == guest_id).with_for_update()
    )
    if guest is None:
        return ClaimResult("not_found")
    if guest.claimed_by_user_id == user_id:
        return ClaimResult("already_claimed")
    if guest.claimed_by_user_id is not None:
        return ClaimResult("claimed_by_other")

    moved = await session.execute(
        update(Conversation)
        .where(Conversation.guest_id == guest_id)
        .values(user_id=user_id, guest_id=None)
    )

    day = quota.today()
    g_subject = guest_subject(guest_id)
    guest_tokens = await session.scalar(
        select(UsageDaily.tokens).where(UsageDaily.subject == g_subject, UsageDaily.day == day)
    ) or 0
    await quota.add_usage(session, user_subject(user_id), guest_tokens, day=day)
    await session.execute(delete(UsageDaily).where(UsageDaily.subject == g_subject))

    guest.claimed_by_user_id = user_id
    guest.claimed_at = datetime.now(UTC)
    return ClaimResult("claimed", conversations_moved=moved.rowcount, tokens_merged=guest_tokens)
