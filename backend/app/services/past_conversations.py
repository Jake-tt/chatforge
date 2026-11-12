"""Search over the user's own earlier conversations ("what did we decide last time?").

Candidates are the latest messages of the owner's most recently active conversations,
excluding the current one (already in the model's history) and deleted ones. They are
ranked with the same BM25 as the help center, built per request, so what one request reads
is bounded: at most the last `messages_per_conversation` seqs of each conversation (fewer
if a turn was undone), each cut to `message_chars`. A few hundred short messages index in
well under a millisecond, so no persistent index is needed at this scale. A managed
full-text or vector index would replace this behind the same function.

The owner filter comes from the caller (`conversations._owned_by`), so ownership rules live
in one place and other users' conversations can never be candidates.
"""

import uuid

from sqlalchemy import ColumnElement, func, select
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
    messages_per_conversation: int,
    message_chars: int,
) -> list[SearchHit]:
    recent = (
        select(Conversation.id)
        .where(owned_by, Conversation.id != current_id)
        .order_by(Conversation.updated_at.desc())
        .limit(conversations)
        .scalar_subquery()
    )
    # The newest seqs of each conversation (message_count is its last seq): an index range
    # read, so a long conversation costs no more than a short one. Cut in the database too.
    rows = await session.execute(
        select(Message.conversation_id, Message.seq, Message.role,
               func.substr(Message.content, 1, message_chars), Conversation.title)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Message.conversation_id.in_(recent),
               Message.seq > Conversation.message_count - messages_per_conversation)
    )
    documents = [
        Document(
            id=f"{conversation_id}#{seq}",
            title=f"지난 대화 › {title or '제목 없음'}",
            url=f"/conversations/{conversation_id}",
            text=f"{_ROLE_LABELS.get(role, role)}: {content}",
        )
        for conversation_id, seq, role, content, title in rows
    ]
    if not documents:
        return []
    return await BM25Search(documents).search(query, limit)
