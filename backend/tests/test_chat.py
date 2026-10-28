from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from tests.fakes import RecordingChatModel, ScriptedChatModel


async def test_stream_reply_is_persisted_and_charged(h):
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    result = await h.chat(headers, cid, "hello there")

    assert result.response.status_code == 200
    assert result.text == "(fake) You said: hello there"
    done = result.last("done")
    assert done["stop_reason"] == "end_turn"
    # 5 answer words + 5 from the fake router's structured call.
    assert done["output_tokens"] == 10

    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "hello there"),
        ("assistant", "(fake) You said: hello there"),
    ]
    usage = (await h.client.get("/usage", headers=headers)).json()
    # Output costs 5x input with the default weights.
    assert done["billed_tokens"] == done["input_tokens"] + 5 * done["output_tokens"]
    assert usage["used"] == done["billed_tokens"]


async def test_first_message_triggers_background_title(h):
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    await h.chat(headers, cid, "how do I bake sourdough bread at home")
    await h.ctx.tasks.drain()

    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    # FakeProvider "summarises" by keeping the last six words.
    assert detail["title"] == "I bake sourdough bread at home"


async def test_history_is_sent_and_windowed(make_harness):
    llm = RecordingChatModel()
    h = await make_harness(chat_model=llm, history_limit=4)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    await h.chat(headers, cid, "one")
    await h.chat(headers, cid, "two")
    await h.chat(headers, cid, "three")

    assert llm.contents(1) == ["one", "reply 1", "two"]
    # The last 4 messages start with "reply 1"; it is trimmed so a user turn leads.
    assert llm.turns(2) == [("user", "two"), ("assistant", "reply 2"), ("user", "three")]


async def test_conversations_are_isolated_between_owners(h):
    alice = await h.user("alice@example.com")
    bob = await h.guest()
    cid = await h.new_conversation(alice)

    assert (await h.client.get(f"/conversations/{cid}", headers=bob)).status_code == 404
    assert (await h.chat(bob, cid, "hi")).response.status_code == 404
    assert (await h.client.get("/conversations", headers=bob)).json() == []


async def test_deleted_conversation_is_hidden(h):
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    assert (await h.client.delete(f"/conversations/{cid}", headers=headers)).status_code == 204
    assert (await h.client.get(f"/conversations/{cid}", headers=headers)).status_code == 404
    assert (await h.client.get("/conversations", headers=headers)).json() == []


async def test_daily_quota_blocks_before_streaming(make_harness):
    h = await make_harness(guest_daily_tokens=10)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    first = await h.chat(headers, cid, "a fairly long message to use up the tiny quota")
    assert first.response.status_code == 200

    second = await h.chat(headers, cid, "again")
    assert second.response.status_code == 429
    assert second.response.json()["detail"]["code"] == "quota_exceeded"


async def test_per_minute_rate_limit(make_harness):
    h = await make_harness(requests_per_minute=2)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    assert (await h.chat(headers, cid, "1")).response.status_code == 200
    assert (await h.chat(headers, cid, "2")).response.status_code == 200
    r = (await h.chat(headers, cid, "3")).response
    assert r.status_code == 429
    assert r.headers["retry-after"] == "60"


PARTIAL = "partial answer " * 8  # past the agent's holdback, so the client saw it


class FailingMidStream(ScriptedChatModel):
    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content=PARTIAL))
        raise ConnectionError("upstream overloaded")


async def test_llm_failure_mid_stream_keeps_partial_and_charges_estimate(make_harness):
    h = await make_harness(chat_model=FailingMidStream())
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    result = await h.chat(headers, cid, "question")

    assert result.text == PARTIAL
    assert result.last("error") == {
        "code": "llm_unavailable", "message": "The model is unavailable, please retry",
        "retryable": True,
    }
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert detail["messages"][-1]["content"] == PARTIAL
    assert (await h.client.get("/usage", headers=headers)).json()["used"] > 0
