import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import ColumnElement, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.graph import AgentContext
from app.agent.stream import AgentStream
from app.agent.tools import build_tools
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
from app.services import summary
from app.services.chat_models import is_retryable
from app.services.history import HistoryEntry
from app.services.llm import ChatTurn, LLMError, Usage
from app.services.past_conversations import search_past_conversations
from app.services.rag import PastSearch, account_snapshot
from app.services.search import SearchHit

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
    cache_read_tokens, cache_write_tokens, billed_tokens, stop_reason} or `error`
    {code, message, retryable}. `billed_tokens` is what the daily quota was charged.
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
    boundary = summary.summarized_through(settings, conversation)
    turns = _to_turns([e for e in recent if e.seq > boundary])
    is_first_message = seq == 1 and conversation.title is None
    system = summary.system_prompt(settings, conversation)
    # The quota just checked is the user's current usage; no need to read it again.
    account = account_snapshot(principal.plan, quota) if settings.account_context_enabled \
        else None
    past = _past_search(ctx, principal, conversation.id) if settings.past_search_enabled \
        else None
    job = ReplyJob(ctx, principal, conversation.id, system, turns, body.content,
                   is_first_message, account, past)
    return StreamingResponse(
        _stream_agent_reply(job),
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


def _past_search(ctx: AppContext, principal: Principal,
                 conversation_id: uuid.UUID) -> PastSearch:
    async def search(query: str) -> list[SearchHit]:
        # Own session: runs concurrently with the help search, after the request's closed.
        async with ctx.sessionmaker() as session:
            return await search_past_conversations(
                session, _owned_by(principal), conversation_id, query,
                conversations=ctx.settings.past_search_conversations,
                limit=ctx.settings.past_search_top_k,
            )
    return search


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _estimate_tokens(text: str) -> int:
    return max(len(text) // 4, 1)


@dataclass(frozen=True)
class ReplyJob:
    """Everything one assistant reply needs, captured before the request session closes."""

    ctx: AppContext
    principal: Principal
    conversation_id: uuid.UUID
    system: str
    turns: list[ChatTurn]
    first_message: str
    is_first_message: bool
    account: SearchHit | None
    past: PastSearch | None


def _estimated_usage(job: ReplyJob, turns: list[ChatTurn], partial: str, aux: Usage) -> Usage:
    # Aborted streams don't report final usage; charge an estimate so that disconnecting
    # mid-reply can't be used to dodge the quota.
    prompt = job.system + "".join(t.context + t.content for t in turns)
    return aux + Usage(_estimate_tokens(prompt), _estimate_tokens(partial))


async def _stream_agent_reply(job: ReplyJob) -> AsyncIterator[str]:
    """Answer one turn with the LangGraph agent, as SSE.

    `AgentStream` folds the graph's `messages` (tokens) and `updates` (state deltas) into
    events; this function only drives the graph, handles failure, and finishes the reply.
    """
    ctx = job.ctx
    stream = AgentStream(turns=job.turns)
    try:
        # Flush headers now; routing and lookups take a moment before the first token.
        yield ": routing\n\n"
        # The deadline guards only the waits on the graph, never a `yield`: a timeout
        # firing while the client is being written to would cancel the whole request.
        deadline = asyncio.get_running_loop().time() + ctx.settings.agent_turn_timeout_seconds
        async with aclosing(ctx.agent.astream(
            {"turns": job.turns, "citations": [], "scratch": [], "tool_rounds": 0,
             "usage": Usage()},
            context=_agent_context(job), stream_mode=["messages", "updates"],
        )) as graph_events:
            while True:
                async with asyncio.timeout_at(deadline):
                    try:
                        mode, payload = await anext(graph_events)
                    except StopAsyncIteration:
                        break
                if mode == "messages":
                    events = stream.on_message(*payload)
                else:
                    events = [e for update in payload.values() if update
                              for e in stream.on_update(update)]
                for name, data in events:
                    yield _sse(name, data)
    except (asyncio.CancelledError, GeneratorExit):
        partial = stream.partial
        await asyncio.shield(_persist_reply(
            job, partial, _estimated_usage(job, stream.turns, partial, stream.usage),
            stream.citations or None))
        raise
    except Exception as exc:
        partial = stream.partial
        yield await _fail_reply(
            job, partial, _estimated_usage(job, stream.turns, partial, stream.usage),
            stream.citations or None, _as_llm_error(exc))
        return

    async for event in _finish_reply(
        job, stream.turns, stream.text, stream.usage, stream.citations or None,
        stream.stop_reason, followups=not stream.clarified, summarize=not stream.clarified,
    ):
        yield event


def _agent_context(job: ReplyJob) -> AgentContext:
    ctx, s = job.ctx, job.ctx.settings
    return AgentContext(
        model=ctx.chat_model, rag=ctx.rag,
        tools=build_tools(s, ctx.rag, job.account, job.past),
        system=job.system, account=job.account, past=job.past,
        max_tool_rounds=s.agent_max_tool_rounds, max_tool_calls=s.agent_max_tool_calls,
        timeout_seconds=s.agent_timeout_seconds,
        prompt_cache=s.llm_provider == "anthropic",
    )


def _as_llm_error(exc: Exception) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    return LLMError(str(exc) or type(exc).__name__, retryable=is_retryable(exc))


async def _fail_reply(job: ReplyJob, partial: str, usage: Usage, citations: list[dict] | None,
                      exc: LLMError) -> str:
    log.warning("llm stream failed: %s", exc)
    await _persist_reply(job, partial, usage, citations)
    return _sse("error", {"code": "llm_unavailable",
                          "message": "The model is unavailable, please retry",
                          "retryable": exc.retryable})


def _wants_followups(ctx: AppContext, answer: str, stop_reason: str | None,
                     followups: bool) -> bool:
    return followups and ctx.suggestions is not None and bool(answer) \
        and stop_reason != "refusal"


async def _finish_reply(job: ReplyJob, turns: list[ChatTurn], answer: str, usage: Usage,
                        citations: list[dict] | None, stop_reason: str | None, *,
                        followups: bool = True, summarize: bool = True,
                        ) -> AsyncIterator[str]:
    """Suggestions, storage and billing, background tasks, then `done`.

    A clarifying turn passes `followups=False, summarize=False`: suggestions would answer
    for the user, and summarizing would fold the open question away.
    """
    ctx = job.ctx
    if _wants_followups(ctx, answer, stop_reason, followups):
        questions, suggestion_usage = await ctx.suggestions.generate(turns, answer)
        usage += suggestion_usage
        if questions:
            yield _sse("suggestions", {"questions": questions})
    message_id = await _persist_reply(job, answer, usage, citations)
    if job.is_first_message:
        await ctx.tasks.enqueue("generate_title", {
            "conversation_id": str(job.conversation_id), "first_message": job.first_message,
        })
    if summarize and message_id is not None and summary.needs_summary(
        ctx.settings, [*turns, ChatTurn("assistant", answer)]
    ):
        try:
            await ctx.tasks.enqueue(summary.TASK_NAME,
                                    {"conversation_id": str(job.conversation_id)})
        except Exception:
            # Only an optimisation; the next turn enqueues it again. The reply is stored
            # and charged, so the client must still get `done`.
            log.warning("could not enqueue conversation summary", exc_info=True)
    yield _sse("done", {
        "message_id": str(message_id),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "billed_tokens": usage.billed(ctx.settings.token_weights()),
        "stop_reason": stop_reason,
    })


async def _persist_reply(
    job: ReplyJob,
    text: str,
    usage: Usage,
    citations: list[dict] | None,
) -> uuid.UUID | None:
    """Store the assistant message and charge its tokens in one transaction.

    With no text (failed or cancelled before the first token) only the tokens already
    spent on routing/search are charged, and no empty message is stored.
    """
    ctx, principal, conversation_id = job.ctx, job.principal, job.conversation_id
    billed = usage.billed(ctx.settings.token_weights())
    if not text and not billed:
        return None
    # A fresh session: the request-scoped one is closed once streaming starts.
    async with ctx.sessionmaker() as session:
        message = None
        if text:
            seq = await _allocate_seq(session, conversation_id, updated_at=datetime.now(UTC))
            message = Message(
                conversation_id=conversation_id, seq=seq, role="assistant", content=text,
                input_tokens=usage.prompt_tokens, output_tokens=usage.output_tokens,
                citations=citations,
            )
            session.add(message)
        await ctx.quota.add_usage(session, principal.subject, billed)
        await session.commit()
    await ctx.quota.invalidate(principal.subject)
    if message is None:
        return None
    await ctx.history.append(principal.subject, conversation_id,
                             HistoryEntry(message.seq, "assistant", text))
    return message.id
