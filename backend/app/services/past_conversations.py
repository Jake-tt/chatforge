"""Search over the user's own earlier conversations ("what did we decide last time?").

Candidates are the messages of the owner's most recently active conversations, excluding
the current one (already in the model's history) and deleted ones. They are ranked with
the same BM25 as the help center, built per request: a few hundred short messages index in
well under a millisecond, so no persistent index is needed at this scale. A managed
full-text or vector index would replace this behind the same function.

The owner filter comes from the caller (`conversations._owned_by`), so ownership rules live
in one place and other users' conversations can never be candidates.
"""

import uuid

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.services.search import BM25Search, Document, SearchHit

_ROLE_LABELS = {"user": "사용자", "assistant": "어시스턴트"}


async def search_past_conversations(
    session: AsyncSession,
    owned_by: ColumnElement[bool],
    current_id: uuid.UUID,
    query: str,
    *,
    conversations: int,
    limit: int,
) -> list[SearchHit]:
    recent = (
        select(Conversation.id)
        .where(owned_by, Conversation.id != current_id)
        .order_by(Conversation.updated_at.desc())
        .limit(conversations)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(Message, Conversation.title)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Message.conversation_id.in_(recent))
    )
    documents = [
        Document(
            id=f"{m.conversation_id}#{m.seq}",
            title=f"지난 대화 › {title or '제목 없음'}",
            url=f"/conversations/{m.conversation_id}",
            text=f"{_ROLE_LABELS.get(m.role, m.role)}: {m.content}",
        )
        for m, title in rows
    ]
    if not documents:
        return []
    return await BM25Search(documents).search(query, limit)
