import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import ColumnElement, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.context import AppContext
from app.deps import Principal, get_ctx, get_principal, get_session
from app.errors import ApiError
from app.models import Conversation, Message
from app.schemas import (
    BulkDeleteOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    MessageCreate,
)
from app.services.history import HistoryEntry
from app.services.llm import ChatTurn, Completion, LLMError, TextDelta, Usage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _owned_by(principal: Principal) -> ColumnElement[bool]:
    owner_col = Conversation.user_id if principal.kind == "user" else Conversation.guest_id
    return and_(owner_col == principal.id, Conversation.deleted_at.is_(None))


async def _get_owned(
    session: AsyncSession, principal: Principal, conversation_id: uuid.UUID, with_messages=False
) -> Conversation:
    stmt = select(Conversation).where(Conversation.id == conversation_id, _owned_by(principal))
    if with_messages:
        stmt = stmt.options(selectinload(Conversation.messages))
    conversation = await session.scalar(stmt)
    if conversation is None:
        # 404 rather than 403 so ids of other users' conversations aren't confirmed.
        raise ApiError(404, "not_found", "Conversation not found")
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)
) -> list[Conversation]:
    rows = await session.scalars(
        select(Conversation)
        .where(_owned_by(principal))
        .order_by(Conversation.updated_at.desc())
        .limit(50)
    )
    return list(rows)


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    owner = {"user_id": principal.id} if principal.kind == "user" else {"guest_id": principal.id}
    conversation = Conversation(title=body.title, **owner)
    session.add(conversation)
    await session.commit()
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    return await _get_owned(session, principal, conversation_id, with_messages=True)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conversation = await _get_owned(session, principal, conversation_id)
    conversation.deleted_at = datetime.now(UTC)
    await session.commit()
    await ctx.history.invalidate(principal.subject, conversation.id)
    return Response(status_code=204)


@router.delete("", response_model=BulkDeleteOut)
async def delete_all_conversations(
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> BulkDeleteOut:
    result = await session.execute(
        update(Conversation).where(_owned_by(principal)).values(deleted_at=datetime.now(UTC))
    )
    await session.commit()
    # Pattern delete over this owner's cache keys, via SCAN (never KEYS).
    await ctx.history.invalidate_owner(principal.subject)
    return BulkDeleteOut(deleted=result.rowcount)


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Store the user message, then stream the assistant reply as Server-Sent Events.

    Events: `delta` {text} … then `done` {message_id, input_tokens, output_tokens,
    stop_reason} or `error` {code, message, retryable}.
    Quota/rate-limit failures are returned as plain HTTP 429 *before* the stream starts.
    """
    conversation = await _get_owned(session, principal, conversation_id)
    settings = ctx.settings

    if await ctx.quota.hit_rate_limit(f"msg:{principal.subject}", settings.requests_per_minute, 60):
        raise ApiError(429, "rate_limited", "Too many messages, slow down",
                       headers={"Retry-After": "60"})
    quota = await ctx.quota.status(
        session, principal.subject, settings.daily_token_limit(principal.plan)
    )
    if quota.exceeded:
        raise ApiError(429, "quota_exceeded", "Daily token limit reached")

    seq = await _allocate_seq(session, conversation.id)
    session.add(Message(conversation_id=conversation.id, seq=seq, role="user",
                        content=body.content))
    await session.commit()
    await ctx.history.append(principal.subject, conversation.id,
                             HistoryEntry(seq, "user", body.content))

    recent = await ctx.history.recent(session, principal.subject, conversation.id, seq)
    is_first_message = seq == 1 and conversation.title is None
    turns = _to_turns(recent)
    return StreamingResponse(
        _stream_reply(ctx, principal, conversation.id, turns, body.content, is_first_message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _allocate_seq(session: AsyncSession, conversation_id: uuid.UUID, **values) -> int:
    """Atomically bump message_count and return it as the new message's seq.

    The UPDATE takes a row lock, so concurrent messages in one conversation get distinct,
    gap-free numbers.
    """
    return await session.scalar(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(message_count=Conversation.message_count + 1, **values)
        .returning(Conversation.message_count)
    )


def _to_turns(entries: list[HistoryEntry]) -> list[ChatTurn]:
    turns = [e.to_turn() for e in entries if e.content]
    # The history window may start mid-exchange; the API requires a user turn first.
    while turns and turns[0].role != "user":
        turns.pop(0)
    return turns


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _estimate_tokens(text: str) -> int:
    return max(len(text) // 4, 1)


async def _stream_reply(
    ctx: AppContext,
    principal: Principal,
    conversation_id: uuid.UUID,
    turns: list[ChatTurn],
    first_message: str,
    is_first_message: bool,
) -> AsyncIterator[str]:
    parts: list[str] = []
    completion: Completion | None = None
    system = ctx.settings.system_prompt
    aux_usage = Usage()  # routing + source compression, charged with the answer
    citations: list[dict] | None = None

    def estimated_usage() -> Usage:
        # Aborted streams don't report final usage; charge an estimate so that
        # disconnecting mid-reply can't be used to dodge the quota.
        prompt = system + "".join(t.content for t in turns)
        return aux_usage + Usage(_estimate_tokens(prompt), _estimate_tokens("".join(parts)))

    try:
        if ctx.rag is not None:
            # Flush headers now; routing and search take a moment before the first token.
            yield ": grounding\n\n"
            result = await ctx.rag.ground(turns)
            aux_usage = result.usage
            if result.grounding is not None:
                system += result.grounding.context
                citations = [c.as_dict() for c in result.grounding.citations]
                yield _sse("sources", {"citations": citations})

        async with aclosing(ctx.llm.stream(system, turns)) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    yield _sse("delta", {"text": event.text})
                else:
                    completion = event
    except LLMError as exc:
        log.warning("llm stream failed: %s", exc)
        await _persist_reply(ctx, principal, conversation_id, "".join(parts),
                             estimated_usage(), citations)
        yield _sse("error", {"code": "llm_unavailable",
                             "message": "The model is unavailable, please retry",
                             "retryable": exc.retryable})
        return
    except (asyncio.CancelledError, GeneratorExit):
        # Shielded so the write survives the cancellation of this request.
        await asyncio.shield(_persist_reply(
            ctx, principal, conversation_id, "".join(parts), estimated_usage(), citations
        ))
        raise

    assert completion is not None, "provider must finish with a Completion"
    usage = aux_usage + Usage(completion.input_tokens, completion.output_tokens)
    message_id = await _persist_reply(
        ctx, principal, conversation_id, "".join(parts), usage, citations
    )
    if is_first_message:
        await ctx.tasks.enqueue("generate_title", {
            "conversation_id": str(conversation_id), "first_message": first_message,
        })
    yield _sse("done", {
        "message_id": str(message_id),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "stop_reason": completion.stop_reason,
    })


async def _persist_reply(
    ctx: AppContext,
    principal: Principal,
    conversation_id: uuid.UUID,
    text: str,
    usage: Usage,
    citations: list[dict] | None,
) -> uuid.UUID | None:
    """Store the assistant message and charge its tokens in one transaction.

    With no text (failed or cancelled before the first token) only the tokens already
    spent on routing/search are charged, and no empty message is stored.
    """
    if not text and not usage.total:
        return None
    # A fresh session: the request-scoped one is closed once streaming starts.
    async with ctx.sessionmaker() as session:
        message = None
        if text:
            seq = await _allocate_seq(session, conversation_id, updated_at=datetime.now(UTC))
            message = Message(
                conversation_id=conversation_id, seq=seq, role="assistant", content=text,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                citations=citations,
            )
            session.add(message)
        await ctx.quota.add_usage(session, principal.subject, usage.total)
        await session.commit()
    await ctx.quota.invalidate(principal.subject)
    if message is None:
        return None
    await ctx.history.append(principal.subject, conversation_id,
                             HistoryEntry(message.seq, "assistant", text))
    return message.id
