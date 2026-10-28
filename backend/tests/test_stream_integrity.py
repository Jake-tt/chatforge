"""Turn integrity: a turn that produced nothing visible is undone, held-back text is still
billed, and a conversation streams one turn at a time."""

import asyncio
import json
import uuid

import fakeredis
import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import update

from app.deps import Principal
from app.errors import ApiError
from app.main import create_app
from app.models import Conversation, Message
from app.routers.conversations import ReplyJob, _stream_agent_reply, _survive_cancellation
from app.services.chat_models import LangChainProvider
from app.services.llm import ChatTurn, FakeProvider
from app.services.turn_gate import Lease, TurnGate
from tests.fakes import ScriptedChatModel
from tests.test_agent import _agent_harness, _answer, _route
from tests.test_internal_tasks import RecordingQueue


async def _messages(h, headers, cid) -> list[tuple[str, str]]:
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    return [(m["role"], m["content"]) for m in detail["messages"]]


async def _used(h, headers) -> int:
    return (await h.client.get("/usage", headers=headers)).json()["used"]


async def test_a_failure_before_anything_was_shown_undoes_the_user_turn(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(), _route()],
        agent=[ConnectionError("model server down"), _answer("답")])

    failed = await h.chat(headers, cid, "첫 질문")

    assert failed.last("error")["code"] == "llm_unavailable"
    assert await _messages(h, headers, cid) == []
    # Asked again, it is the conversation's first message: it still gets a title.
    retried = await h.chat(headers, cid, "첫 질문")
    assert retried.text == "답"
    assert await _messages(h, headers, cid) == [("user", "첫 질문"), ("assistant", "답")]
    # The undone question isn't sent twice to the model.
    assert [m.content for m in agent.calls[-1]["messages"][1:]] == ["첫 질문"]
    assert "generate_title" in [name for name, _ in h.ctx.tasks.enqueued]


class FailsAfterHeldText(ScriptedChatModel):
    """Writes less than the holdback, so nothing reaches the client, then fails."""

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="가" * 60))
        raise ConnectionError("dropped")


async def _one_failed_turn(make_harness, chat_model) -> tuple[list, int]:
    """Messages left and tokens charged after one turn whose answering model fails."""
    router = ScriptedChatModel(script=[_route()])
    h = await make_harness(llm=LangChainProvider(router), chat_model=chat_model,
                           rag_enabled=True, tasks=RecordingQueue())
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    result = await h.chat(headers, cid, "질문")
    assert result.last("error")["code"] == "llm_unavailable"
    return await _messages(h, headers, cid), await _used(h, headers)


async def test_text_held_back_before_a_failure_is_still_billed(make_harness):
    _, router_only = await _one_failed_turn(
        make_harness, ScriptedChatModel(script=[ConnectionError("down")]))

    messages, used = await _one_failed_turn(make_harness, FailsAfterHeldText())

    assert messages == []  # nothing was shown, so the turn is undone
    assert used > router_only  # but the 60 generated characters are paid for


async def test_a_disconnect_before_anything_was_shown_undoes_the_user_turn(make_harness):
    h, _, headers, cid = await _agent_harness(make_harness, routes=[_route()],
                                              agent=[_answer("가" * 200)])
    me = (await h.client.get("/auth/me", headers=headers)).json()
    conversation_id = uuid.UUID(cid)
    async with h.ctx.sessionmaker() as session:  # the user turn, as send_message stores it
        session.add(Message(conversation_id=conversation_id, seq=1, role="user",
                            content="질문"))
        await session.execute(update(Conversation)
                              .where(Conversation.id == conversation_id)
                              .values(message_count=1))
        await session.commit()
    job = ReplyJob(h.ctx, Principal("guest", uuid.UUID(me["id"]), "guest"), conversation_id,
                   h.ctx.settings.system_prompt, [ChatTurn("user", "질문")], "질문", True,
                   None, None, user_seq=1)

    stream = _stream_agent_reply(job)
    await anext(stream)  # the ": routing" comment: headers are out, nothing shown yet
    await stream.aclose()  # the client went away

    assert await _messages(h, headers, cid) == []


async def test_a_conversation_streams_one_turn_at_a_time(make_harness):
    h, _, headers, cid = await _agent_harness(
        make_harness, routes=[_route(), _route()], agent=[_answer("하나"), _answer("둘")],
        delay=0.3)

    first, second = await asyncio.gather(h.chat(headers, cid, "질문 1"),
                                         h.chat(headers, cid, "질문 2"))

    statuses = sorted([first.response.status_code, second.response.status_code])
    assert statuses == [200, 409]
    blocked = first if first.response.status_code == 409 else second
    assert blocked.response.json()["detail"]["code"] == "stream_in_progress"
    # The turn that ran is intact: no interleaved user messages.
    assert [role for role, _ in await _messages(h, headers, cid)] == ["user", "assistant"]


async def test_the_next_turn_can_start_once_one_finishes_or_fails(make_harness):
    h, _, headers, cid = await _agent_harness(
        make_harness, routes=[_route(), _route()],
        agent=[ConnectionError("down"), _answer("답")])

    await h.chat(headers, cid, "1")
    assert (await h.chat(headers, cid, "2")).response.status_code == 200


async def test_concurrent_streams_per_user_are_capped(make_harness):
    # Bounds how far a user can run past the daily quota with parallel turns: the quota
    # is checked when a turn starts and charged when it ends.
    h, _, headers, _ = await _agent_harness(
        make_harness, routes=[_route()] * 4, agent=[_answer("답")] * 4, delay=0.3,
        max_concurrent_streams=2)
    cids = [await h.new_conversation(headers) for _ in range(3)]

    results = await asyncio.gather(*(h.chat(headers, c, "질문") for c in cids))

    codes = sorted(r.response.status_code for r in results)
    assert codes == [200, 200, 429]
    [blocked] = [r for r in results if r.response.status_code == 429]
    assert blocked.response.json()["detail"]["code"] == "too_many_streams"
    # The rejected conversation isn't left locked: it can be retried at once.
    retry = await h.chat(headers, cids[results.index(blocked)], "질문")
    assert retry.response.status_code == 200




async def test_a_crashed_turn_stops_counting_against_the_user_after_the_ttl(make_harness):
    # An instance that dies mid-turn never releases its slot; the slot must age out even if
    # the user keeps chatting (a counter re-armed by each new turn never would).
    h = await make_harness()
    gate = TurnGate(h.ctx.redis_guard, lock_ttl_seconds=1, max_streams=2)
    await gate.acquire("user:x", uuid.uuid4())  # crashed: never released
    await asyncio.sleep(0.6)
    await gate.release(await gate.acquire("user:x", uuid.uuid4()))  # still active

    await asyncio.sleep(0.5)  # past the crashed turn's TTL, not the active one's
    both = [await gate.acquire("user:x", uuid.uuid4()) for _ in range(2)]

    for lease in both:
        await gate.release(lease)


async def test_releasing_a_slot_never_frees_someone_elses(make_harness):
    # A turn admitted while Redis was down holds no slot; releasing it later must not
    # free the slot of a turn that does.
    h = await make_harness()
    gate = TurnGate(h.ctx.redis_guard, lock_ttl_seconds=60, max_streams=1)
    held = await gate.acquire("user:x", uuid.uuid4())
    phantom = Lease("turn:other", "not-ours", "streams:user:x")  # admitted while Redis was down

    await gate.release(phantom)

    with pytest.raises(ApiError, match="Too many replies"):
        await gate.acquire("user:x", uuid.uuid4())
    await gate.release(held)


async def test_a_response_that_never_started_releases_the_turn(make_harness):
    # Starlette sends headers before it runs the body; if that fails (or the client is
    # gone) the body generator never starts and its `finally` never runs.
    h, _, headers, cid = await _agent_harness(make_harness, routes=[_route(), _route()],
                                              agent=[_answer("답"), _answer("답")])
    body = json.dumps({"content": "질문"}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": f"/conversations/{cid}/messages",
             "raw_path": f"/conversations/{cid}/messages".encode(), "query_string": b"",
             "root_path": "", "server": ("test", 80), "client": ("1.2.3.4", 1),
             "headers": [(b"content-type", b"application/json"),
                         (b"authorization", headers["Authorization"].encode())]}
    messages = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(message):
        if message["type"] == "http.response.start":
            raise OSError("client went away")

    with pytest.raises(OSError):
        await h.app(scope, receive, send)

    assert await _messages(h, headers, cid) == []  # the stored user message is undone
    assert (await h.chat(headers, cid, "다시")).response.status_code == 200  # not 409


async def test_an_expired_lease_does_not_release_the_next_turns_lock(make_harness):
    h = await make_harness()
    gate = TurnGate(h.ctx.redis_guard, lock_ttl_seconds=1, max_streams=5)
    conversation = uuid.uuid4()
    stale = await gate.acquire("user:x", conversation)
    await asyncio.sleep(1.1)  # its lock expired; another turn takes the conversation
    current = await gate.acquire("user:x", conversation)

    await gate.release(stale)

    with pytest.raises(ApiError, match="still being written"):
        await gate.acquire("user:x", conversation)
    await gate.release(current)


async def test_a_turn_that_fails_to_start_releases_the_conversation(make_harness):
    h, _, headers, cid = await _agent_harness(make_harness, routes=[_route()],
                                              agent=[_answer("답")])
    me = (await h.client.get("/auth/me", headers=headers)).json()
    # A corrupt history cache entry makes the turn fail after admission, before streaming.
    await h.ctx.redis.rpush(f"history:guest:{me['id']}:{cid}", "not json")

    with pytest.raises(json.JSONDecodeError):  # the test client re-raises app errors
        await h.chat(headers, cid, "질문")

    await h.ctx.redis.delete(f"history:guest:{me['id']}:{cid}")
    assert (await h.chat(headers, cid, "다시")).response.status_code == 200  # not 409


async def test_shutdown_waits_for_writes_still_in_flight(make_settings):
    # On SIGTERM the lifespan closes the database and Redis; a reply still being saved for
    # a cancelled request must finish first, or its text and its charge are lost.
    app = create_app(make_settings(), redis=fakeredis.FakeAsyncRedis(decode_responses=True),
                     llm=FakeProvider())
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    saved: list[bool] = []

    async def slow_save():
        await asyncio.sleep(0.1)
        saved.append(True)

    request = asyncio.ensure_future(_survive_cancellation(slow_save()))
    await asyncio.sleep(0)
    request.cancel()  # the client went away; the save carries on

    await lifespan.__aexit__(None, None, None)

    assert saved == [True]
