"""Turn integrity: a turn that produced nothing visible is undone, held-back text is still
billed, and a conversation streams one turn at a time."""

import asyncio
import uuid

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import update

from app.deps import Principal
from app.models import Conversation, Message
from app.routers.conversations import ReplyJob, _stream_agent_reply
from app.services.chat_models import LangChainProvider
from app.services.llm import ChatTurn
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
        make_harness, routes=[_route()] * 3, agent=[_answer("답")] * 3, delay=0.3,
        max_concurrent_streams=2)
    cids = [await h.new_conversation(headers) for _ in range(3)]

    results = await asyncio.gather(*(h.chat(headers, c, "질문") for c in cids))

    codes = sorted(r.response.status_code for r in results)
    assert codes == [200, 200, 429]
    [blocked] = [r for r in results if r.response.status_code == 429]
    assert blocked.response.json()["detail"]["code"] == "too_many_streams"
