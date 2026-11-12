import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import ColumnElement, and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.graph import AgentContext
from app.agent.stream import AgentStream
from app.agent.tools import MemoryTools, WebSearch, build_tools
from app.context import AppContext
from app.deps import Principal, get_ctx, get_principal, get_session
from app.errors import ApiError
from app.models import Attachment, Conversation, Message
from app.schemas import (
    AttachmentStatusOut,
    BulkDeleteOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    MessageCreate,
)
from app.services import attachments, memory, memory_extraction, summary
from app.services.attachments import (
    accept_upload,
    has_cut_documents,
    images_for_turns,
    render,
    search_attachments,
    turn_order,
)
from app.services.chat_models import is_retryable
from app.services.history import HistoryEntry
from app.services.llm import ChatTurn, ImagePart, LLMError, Usage
from app.services.past_conversations import search_past_conversations
from app.services.rag import PastSearch, account_snapshot, check_citations
from app.services.recap import Recap, recap_for
from app.services.search import SearchHit
from app.services.turn_gate import Lease

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
        stmt = stmt.options(selectinload(Conversation.messages).selectinload(Message.attachments))
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


@router.get("/{conversation_id}/recap", response_model=Recap,
            responses={204: {"description": "No recap: not left long enough, or too short"}})
async def get_recap(
    conversation_id: uuid.UUID,
    ctx: AppContext = Depends(get_ctx),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Recap | Response:
    conversation = await _get_owned(session, principal, conversation_id)
    return await recap_for(ctx, session, conversation) or Response(status_code=204)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conversation = await _get_owned(session, principal, conversation_id)
    conversation.deleted_at = datetime.now(UTC)
    # What was remembered only from this conversation goes with it.
    await memory.forget_conversations(session, [conversation.id])
    await session.commit()
    await ctx.history.invalidate(principal.subject, conversation.id)
    return Response(status_code=204)


@router.delete("", response_model=BulkDeleteOut)
async def delete_all_conversations(
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> BulkDeleteOut:
    ids = list(await session.scalars(select(Conversation.id).where(_owned_by(principal))))
    result = await session.execute(
        update(Conversation).where(_owned_by(principal)).values(deleted_at=datetime.now(UTC))
    )
    await memory.forget_conversations(session, ids)
    await session.commit()
    # Pattern delete over this owner's cache keys, via SCAN (never KEYS).
    await ctx.history.invalidate_owner(principal.subject)
    return BulkDeleteOut(deleted=result.rowcount)


def _attachment_status(attachment: Attachment) -> AttachmentStatusOut:
    return AttachmentStatusOut(
        id=attachment.id, filename=attachment.filename, kind=attachment.kind,
        status=attachment.status, error_code=attachment.error_code,
        chars=len(attachment.markdown) if attachment.markdown is not None else None)


@router.post("/{conversation_id}/attachments", response_model=AttachmentStatusOut,
             responses={202: {"description": "Accepted; converted in the background"}})
async def upload_attachment(
    conversation_id: uuid.UUID,
    file: UploadFile,
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    conversation = await _get_owned(session, principal, conversation_id)
    if await ctx.quota.hit_rate_limit(f"upload:{principal.subject}",
                                      ctx.settings.uploads_per_minute, 60):
        raise ApiError(429, "rate_limited", "Too many uploads, slow down",
                       headers={"Retry-After": "60"})
    # One byte over the limit is enough to refuse it without reading the rest.
    data = await file.read(ctx.settings.attachment_max_bytes + 1)
    attachment = await accept_upload(session, ctx.settings, conversation,
                                     file.filename or "", data)
    await session.commit()
    if attachment.status == "processing":
        try:
            await ctx.tasks.enqueue(attachments.TASK_NAME,
                                    {"attachment_id": str(attachment.id)})
        except Exception:
            # Left "processing" it would never finish: fail it so the user uploads again.
            log.warning("could not enqueue attachment conversion", exc_info=True)
            attachment.status, attachment.error_code = "failed", "queue_unavailable"
            attachment.pending_data = None
            await session.commit()
            raise ApiError(503, "queue_unavailable", "The file couldn't be queued, retry") \
                from None
    code = 202 if attachment.status == "processing" else 201
    return JSONResponse(_attachment_status(attachment).model_dump(mode="json"),
                        status_code=code)


@router.get("/{conversation_id}/attachments/{attachment_id}",
            response_model=AttachmentStatusOut)
async def get_attachment(
    conversation_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> AttachmentStatusOut:
    conversation = await _get_owned(session, principal, conversation_id)
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None or attachment.conversation_id != conversation.id:
        raise ApiError(404, "not_found", "Attachment not found")
    return _attachment_status(attachment)


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Store the user message, then stream the assistant reply as Server-Sent Events.

    Events: `delta` {text} … then `done` {message_id, input_tokens, output_tokens, cited,
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

    lease = await ctx.turns.acquire(principal.subject, conversation.id)
    try:
        return await _start_turn(ctx, principal, conversation, body, quota, session, lease)
    except BaseException:
        await ctx.turns.release(lease)  # the stream never started, so it can't release
        raise


async def _start_turn(ctx: AppContext, principal: Principal, conversation: Conversation,
                      body: MessageCreate, quota, session: AsyncSession,
                      lease: Lease) -> StreamingResponse:
    settings = ctx.settings
    attached = await _unsent_attachments(session, conversation.id, body.attachment_ids)
    seq = await _allocate_seq(session, conversation.id)
    message = Message(id=uuid.uuid4(), conversation_id=conversation.id, seq=seq, role="user",
                      content=body.content)
    session.add(message)
    # The message row first: nothing tells the unit of work to order these two (the
    # relationship is view-only), and Postgres checks the foreign key on each statement.
    await session.flush()
    for attachment in attached:
        attachment.message_id = message.id
    await session.commit()
    await ctx.history.append(principal.subject, conversation.id, HistoryEntry(
        seq, "user", body.content,
        attachments=render(turn_order(attached), settings.attachment_inline_chars)))

    recent = await ctx.history.recent(session, principal.subject, conversation.id, seq)
    boundary = summary.summarized_through(settings, conversation)
    window = [e for e in recent if e.seq > boundary]
    # Images go whole only with the latest few user turns; older ones by description.
    recent_users = [e.seq for e in window if e.role == "user"][-settings.image_recent_turns:]
    images = await images_for_turns(session, ctx.blobs, conversation.id, recent_users)
    turns = _to_turns(window, images)
    after_clarify = _follows_clarify(recent, seq)
    is_first_message = seq == 1 and conversation.title is None
    system = summary.system_prompt(settings, conversation)
    # The quota just checked is the user's current usage; no need to read it again.
    account = account_snapshot(principal.plan, quota) if settings.account_context_enabled \
        else None
    past = _past_search(ctx, principal, conversation.id) if settings.past_search_enabled \
        else None
    files = _attachment_search(ctx, conversation.id) if await has_cut_documents(
        session, conversation.id, settings.attachment_inline_chars) else None
    remembers = principal.kind == "user" and await memory.is_enabled(session, principal.id)
    if remembers:
        # The core facts ride on this turn only (context): the system prompt stays cached.
        core = await memory.core_block(session, principal.id, settings.memory_core_chars)
        if core:
            turns[-1] = dataclasses.replace(turns[-1], context=core)
    job = ReplyJob(ctx, principal, conversation.id, system, turns, body.content,
                   is_first_message, account, past, user_seq=seq, lease=lease,
                   after_clarify=after_clarify, attachments=files,
                   memory=_memory_tools(ctx, principal.id, conversation.id)
                   if remembers else None)
    return TurnResponse(job, TurnProgress())


async def _unsent_attachments(session: AsyncSession, conversation_id: uuid.UUID,
                              ids: list[uuid.UUID]) -> list[Attachment]:
    """The attachments a message may carry: uploaded to this conversation, not yet sent."""
    wanted = set(ids)
    if not wanted:
        return []
    rows = list(await session.scalars(select(Attachment).where(
        Attachment.id.in_(wanted), Attachment.conversation_id == conversation_id,
        Attachment.message_id.is_(None), Attachment.status != "failed")))
    if len(rows) != len(wanted):
        raise ApiError(400, "invalid_attachment",
                       "An attachment was not found in this conversation or was already sent")
    if any(a.status != "ready" for a in rows):
        raise ApiError(409, "attachment_not_ready", "An attachment is still being read")
    return rows


async def _undo_user_turn(session: AsyncSession, conversation_id: uuid.UUID, seq: int) -> None:
    """Remove the turn's user message and give its seq back.

    The count only moves back if it still ends at this message: the turn holds the
    conversation's lock, so nothing should have come after it, and if something did
    (the lock failed open) the seq is left as is rather than reused.
    """
    undone = select(Message.id).where(Message.conversation_id == conversation_id,
                                      Message.seq == seq).scalar_subquery()
    # Its files go back to unsent, so the question can be sent again with them.
    await session.execute(update(Attachment).where(Attachment.message_id == undone)
                          .values(message_id=None))
    await session.execute(delete(Message).where(Message.conversation_id == conversation_id,
                                                 Message.seq == seq))
    await session.execute(update(Conversation)
                          .where(Conversation.id == conversation_id,
                                 Conversation.message_count == seq)
                          .values(message_count=seq - 1))


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


def _follows_clarify(recent: list[HistoryEntry], user_seq: int) -> bool:
    """Whether the message just before this turn's user message was a clarifying question."""
    return any(e.seq == user_seq - 1 and e.role == "assistant" and e.kind == "clarify"
               for e in recent)


def _to_turns(entries: list[HistoryEntry],
              images: dict[int, tuple[ImagePart, ...]] | None = None) -> list[ChatTurn]:
    images = images or {}
    turns = [dataclasses.replace(e.to_turn(), images=images.get(e.seq, ()))
             for e in entries if e.content]
    # The history window may start mid-exchange; the API requires a user turn first.
    while turns and turns[0].role != "user":
        turns.pop(0)
    return turns


def _memory_tools(ctx: AppContext, user_id: uuid.UUID,
                  conversation_id: uuid.UUID) -> MemoryTools:
    # Own sessions, like the other lookups: they run after the request's has closed.
    async def remember(subject: str, predicate: str, obj: str, is_name: bool) -> str | None:
        refused = memory.sensitive_reason(f"{subject} {predicate} {obj}")
        if refused:
            return refused
        async with ctx.sessionmaker() as session:
            await memory.add_fact(session, user_id, subject, predicate,
                                  obj=obj if is_name else None,
                                  value=None if is_name else obj,
                                  conversation_id=conversation_id)
            await session.commit()
        return None

    async def recall(query: str) -> list[SearchHit]:
        async with ctx.sessionmaker() as session:
            return await memory.recall(session, user_id, query, ctx.settings.search_top_k)

    async def forget(ids: list[str]) -> int:
        async with ctx.sessionmaker() as session:
            removed = await memory.forget_ids(session, user_id, ids)
            await session.commit()
            return removed

    return MemoryTools(remember=remember, recall=recall, forget=forget)


def _attachment_search(ctx: AppContext, conversation_id: uuid.UUID) -> PastSearch:
    async def search(query: str) -> list[SearchHit]:
        # Own session, like the other lookups: it runs after the request's has closed.
        async with ctx.sessionmaker() as session:
            return await search_attachments(
                session, conversation_id, query,
                inline_chars=ctx.settings.attachment_inline_chars,
                limit=ctx.settings.search_top_k)
    return search


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


def _web_search(ctx: AppContext, principal: Principal) -> WebSearch | None:
    provider = ctx.web_search
    if provider is None:
        return None

    async def search(query: str) -> list[SearchHit] | None:
        limit = ctx.settings.daily_web_searches(principal.plan)
        if not await ctx.quota.take_daily(f"web:{principal.subject}", limit):
            return None
        async with asyncio.timeout(ctx.settings.web_search_timeout_seconds):
            return await provider.search(query, ctx.settings.web_search_top_k)
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
    user_seq: int = 0  # the user message this turn answers; undone if nothing is shown
    lease: Lease | None = None  # admission held for the turn, released when it ends
    after_clarify: bool = False  # the previous assistant turn asked the user back
    attachments: PastSearch | None = None  # set when a document here was cut
    memory: MemoryTools | None = None  # a member with memory on


# Writes that must outlive a cancelled request. `asyncio.shield` keeps no strong reference
# to the task it shields, so a reference is held here until the write finishes.
_UNFINISHED_WRITES: set[asyncio.Task] = set()


async def _survive_cancellation[T](coro: Awaitable[T]) -> T:
    task = asyncio.ensure_future(coro)
    _UNFINISHED_WRITES.add(task)
    task.add_done_callback(_UNFINISHED_WRITES.discard)
    return await asyncio.shield(task)


async def _charge(job: ReplyJob, billed: int) -> None:
    """Charge tokens spent after the reply was stored (its follow-up suggestions)."""
    async with job.ctx.sessionmaker() as session:
        await job.ctx.quota.add_usage(session, job.principal.subject, billed)
        await session.commit()
    await job.ctx.quota.invalidate(job.principal.subject)


async def drain_unfinished_writes() -> None:
    """Let in-flight saves finish before shutdown closes the database and Redis."""
    if _UNFINISHED_WRITES:
        await asyncio.wait(set(_UNFINISHED_WRITES))


@dataclass
class TurnProgress:
    started: bool = False  # the reply generator ran, so its own cleanup will run


class TurnResponse(StreamingResponse):
    """A streamed reply that cleans up even if its body never starts.

    Starlette sends the headers before it runs the body generator. If sending them fails,
    or the client is gone first, the generator never starts, and a generator that never
    started skips its `finally` when closed: the admission lease would stay held and the
    stored user message would be left without a reply.
    """

    def __init__(self, job: ReplyJob, progress: TurnProgress):
        super().__init__(_stream_agent_reply(job, progress), media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        self._job = job
        self._progress = progress

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._progress.started:
                await _survive_cancellation(_abandon(self._job))


async def _abandon(job: ReplyJob) -> None:
    await _persist_reply(job, "", Usage(), None)  # nothing was shown: undo the turn
    if job.lease is not None:
        await job.ctx.turns.release(job.lease)


def _estimated_usage(job: ReplyJob, turns: list[ChatTurn], partial: str, aux: Usage) -> Usage:
    # Aborted streams don't report final usage; charge an estimate so that disconnecting
    # mid-reply can't be used to dodge the quota.
    prompt = job.system + "".join(t.context + t.content for t in turns)
    return aux + Usage(_estimate_tokens(prompt), _estimate_tokens(partial))


async def _stream_agent_reply(job: ReplyJob,
                             progress: TurnProgress | None = None) -> AsyncIterator[str]:
    """Answer one turn with the LangGraph agent, as SSE.

    `AgentStream` folds the graph's `messages` (tokens) and `updates` (state deltas) into
    events; this function only drives the graph, handles failure, and finishes the reply.
    """
    if progress is not None:
        progress.started = True
    try:
        # aclosing: when the client goes away, closing this generator must close the inner
        # one now, so its cancellation handler stores the partial reply before the lease
        # is released (left to the garbage collector, it would run later, out of order).
        async with aclosing(_run_agent(job)) as events:
            async for event in events:
                yield event
    finally:
        if job.lease is not None:
            await _survive_cancellation(job.ctx.turns.release(job.lease))


async def _run_agent(job: ReplyJob) -> AsyncIterator[str]:
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
             "usage": Usage(), "after_clarify": job.after_clarify},
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
        # Store what the client saw; bill everything the model wrote.
        await _survive_cancellation(_persist_reply(
            job, stream.partial,
            _estimated_usage(job, stream.turns, stream.generated, stream.usage),
            stream.citations or None))
        raise
    except Exception as exc:
        yield await _fail_reply(
            job, stream.partial,
            _estimated_usage(job, stream.turns, stream.generated, stream.usage),
            stream.citations or None, _as_llm_error(exc))
        return

    async for event in _finish_reply(
        job, stream.turns, stream.text, stream.usage, stream.citations or None,
        stream.stop_reason, followups=not stream.clarified, summarize=not stream.clarified,
        options=stream.options if stream.clarified else None,
    ):
        yield event


def _agent_context(job: ReplyJob) -> AgentContext:
    ctx, s = job.ctx, job.ctx.settings
    return AgentContext(
        model=ctx.chat_model, rag=ctx.rag,
        tools=build_tools(s, ctx.rag, job.account, job.past, _web_search(ctx, job.principal),
                          job.attachments, job.memory),
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
                        options: list[str] | None = None,
                        ) -> AsyncIterator[str]:
    """Storage and billing, background tasks, clarify options or suggestions, then `done`.

    The answer is stored and charged before anything else is awaited or sent: the client
    has seen all of it, so a disconnect from here on (while suggestions are made, or an
    event is written) must not leave it unstored and unbilled. The suggestions' tokens are
    charged on their own once they're made.

    A clarifying turn passes `followups=False, summarize=False` and its `options`:
    suggestions would answer for the user, and summarizing would fold the open question
    away. Its options take the place of suggestions.
    """
    ctx = job.ctx
    weights = ctx.settings.token_weights()
    message = await _survive_cancellation(
        _persist_reply(job, answer, usage, citations, options=options))
    billed = usage.billed(weights)
    message_id = message.id if message else None
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
    if summarize and message is not None and summary.wants_idle_compaction(ctx.settings):
        try:
            await ctx.tasks.enqueue(
                summary.IDLE_TASK_NAME,
                {"conversation_id": str(job.conversation_id), "after_seq": message.seq},
                delay_seconds=ctx.settings.idle_compaction_seconds)
        except Exception:
            log.warning("could not schedule idle compaction", exc_info=True)
    if job.memory is not None and message is not None and \
            memory_extraction.wants_extraction(ctx):
        await memory_extraction.schedule(ctx, job.conversation_id, message.seq)
    if options is not None:
        yield _sse("clarify", {"options": options})
    if message is not None and _wants_followups(ctx, answer, stop_reason, followups):
        questions, suggestion_usage = await ctx.suggestions.generate(turns, answer)
        if extra := suggestion_usage.billed(weights):
            await _charge(job, extra)
        usage += suggestion_usage
        billed += extra
        if questions:
            yield _sse("suggestions", {"questions": questions})
    yield _sse("done", {
        "message_id": str(message_id),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "billed_tokens": billed,
        "stop_reason": stop_reason,
        "cited": [c["n"] for c in (message.citations if message else None) or []
                  if c["cited"]],
    })


def _checked_citations(job: ReplyJob, text: str, citations: list[dict] | None
                       ) -> list[dict] | None:
    if not citations and "[" not in text:
        return citations
    checked, missing = check_citations(text, citations or [])
    if missing:
        log.warning("answer cites missing sources", extra={
            "missing": missing, "sources": len(checked),
            "conversation_id": str(job.conversation_id)})
    return checked or None


async def _persist_reply(
    job: ReplyJob,
    text: str,
    usage: Usage,
    citations: list[dict] | None,
    *,
    options: list[str] | None = None,
) -> Message | None:
    """Store the assistant message and charge its tokens in one transaction.

    Its citations are stored checked against the text (`check_citations`).

    With no text (the turn failed or was cancelled before anything reached the client)
    the user message is undone, so the conversation is as before the turn: the same
    question can be asked again, and still counts as the first message. Tokens already
    spent are charged either way.
    """
    ctx, principal, conversation_id = job.ctx, job.principal, job.conversation_id
    billed = usage.billed(ctx.settings.token_weights())
    undo = not text and job.user_seq > 0
    if not (text or billed or undo):
        return None
    # A fresh session: the request-scoped one is closed once streaming starts.
    async with ctx.sessionmaker() as session:
        if undo:
            await _undo_user_turn(session, conversation_id, job.user_seq)
        message = None
        if text:
            seq = await _allocate_seq(session, conversation_id, updated_at=datetime.now(UTC))
            if seq is None:
                # The account was deleted and purged while this reply streamed: nothing
                # to store it in, and no one left to charge. Leaving now rolls back.
                log.info("dropping a reply to a purged conversation",
                         extra={"conversation_id": str(conversation_id)})
                return None
            message = Message(
                conversation_id=conversation_id, seq=seq, role="assistant", content=text,
                kind="answer" if options is None else "clarify", options=options,
                input_tokens=usage.prompt_tokens, output_tokens=usage.output_tokens,
                citations=_checked_citations(job, text, citations),
            )
            session.add(message)
        await ctx.quota.add_usage(session, principal.subject, billed)
        await session.commit()
    await ctx.quota.invalidate(principal.subject)
    if message is None:
        if undo:
            await ctx.history.invalidate(principal.subject, conversation_id)
        return None
    await ctx.history.append(principal.subject, conversation_id,
                             HistoryEntry(message.seq, "assistant", text, message.kind))
    return message
