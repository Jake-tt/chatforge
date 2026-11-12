"""User memory in answers: remember/forget on request, the core facts on every turn, and
recall_memory walking the graph.

The answering model is scripted (tool calls included); routing, storage and the graph are
real and driven over HTTP.
"""

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.services import memory
from tests.fakes import ScriptedChatModel, tool_call

MEMBER = "mem@example.com"


async def _member(make_harness, script: list, **settings):
    agent = ScriptedChatModel(script=script)
    h = await make_harness(chat_model=agent, **settings)
    r = await h.signup(MEMBER)
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    return h, agent, headers, uuid.UUID(r.json()["user"]["id"])


async def _seed(h, user_id, cid, subject, predicate, *, obj=None, value=None) -> str:
    async with h.ctx.sessionmaker() as session:
        fact = await memory.add_fact(session, user_id, subject, predicate, obj=obj, value=value,
                                     conversation_id=uuid.UUID(cid))
        await session.commit()
        return str(fact.id)


def _tool_names(agent, call: int = 0) -> list[str]:
    return [t["function"]["name"] for t in agent.calls[call]["tools"]]


def _last_user(agent, call: int = -1) -> str:
    return [m for m in agent.calls[call]["messages"] if isinstance(m, HumanMessage)][-1].content


async def _facts(h, headers) -> list[tuple[str, str, str]]:
    body = (await h.client.get("/memory", headers=headers)).json()
    return [(f["subject"], f["predicate"], f["object"]) for f in body["facts"]]


async def test_what_a_member_asks_to_remember_is_used_in_a_later_conversation(make_harness):
    h, agent, headers, _ = await _member(make_harness, [
        tool_call("remember", subject="나", predicate="좋아하는 음료", object="라떼",
                  object_is_name=False),
        AIMessage("기억할게요."),
        AIMessage("라떼 어떠세요?"),
    ])
    first = await h.new_conversation(headers)
    await h.chat(headers, first, "나 라떼 좋아해, 기억해줘")
    assert await _facts(h, headers) == [("나", "좋아하는 음료", "라떼")]

    later = await h.new_conversation(headers)
    await h.chat(headers, later, "음료 하나 추천해줘")

    turn = _last_user(agent)
    assert "<memory>" in turn and "사용자 → 좋아하는 음료 → 라떼" in turn
    assert "don't cite it with [n]" in turn  # it's no numbered source
    # On the turn, not the system prompt, which stays the same for the prompt cache.
    system = agent.calls[-1]["messages"][0]
    assert isinstance(system, SystemMessage) and "라떼" not in str(system.content)


async def test_recall_walks_from_a_name_to_what_is_connected(make_harness):
    h, agent, headers, user_id = await _member(make_harness, [
        tool_call("recall_memory", query="부산"), AIMessage("해운대 쪽이죠.")])
    cid = await h.new_conversation(headers)
    await _seed(h, user_id, cid, "나", "사는 곳", obj="부산")
    await _seed(h, user_id, cid, "부산", "자주 가는 곳", obj="해운대")
    await _seed(h, user_id, cid, "나", "좋아하는 음료", value="라떼")  # not about 부산

    await h.chat(headers, cid, "내가 사는 동네 얘기 기억나?")

    tool_result = agent.calls[1]["messages"][-1].content  # sent back on the next call
    assert "사용자 → 사는 곳 → 부산" in tool_result
    assert "부산 → 자주 가는 곳 → 해운대" in tool_result
    assert "라떼" not in tool_result


async def test_recall_falls_back_to_facts_when_no_name_matches(make_harness):
    h, agent, headers, user_id = await _member(make_harness, [
        tool_call("recall_memory", query="음료"), AIMessage("라떼요.")])
    cid = await h.new_conversation(headers)
    await _seed(h, user_id, cid, "나", "좋아하는 음료", value="라떼")
    await _seed(h, user_id, cid, "나", "사는 곳", obj="부산")

    await h.chat(headers, cid, "내가 좋아하는 음료 기억나?")

    tool_result = agent.calls[1]["messages"][-1].content
    assert "사용자 → 좋아하는 음료 → 라떼" in tool_result and "부산" not in tool_result


async def test_forget_removes_a_memory_by_the_id_recall_showed(make_harness):
    h, agent, headers, user_id = await _member(make_harness, [])
    cid = await h.new_conversation(headers)
    fact = await _seed(h, user_id, cid, "나", "좋아하는 음료", value="라떼")
    agent.script.extend([tool_call("forget", memory_ids=[fact[:8]]), AIMessage("잊었어요.")])

    await h.chat(headers, cid, "라떼 좋아한다는 거 잊어줘")

    assert await _facts(h, headers) == []


async def test_a_value_that_looks_like_a_card_number_is_not_remembered(make_harness):
    h, agent, headers, _ = await _member(make_harness, [
        tool_call("remember", subject="나", predicate="카드", object="4111 1111 1111 1111",
                  object_is_name=False),
        AIMessage("그건 저장하지 않았어요.")])
    cid = await h.new_conversation(headers)

    await h.chat(headers, cid, "내 카드번호 기억해줘 4111 1111 1111 1111")

    assert "can't be stored" in agent.calls[1]["messages"][-1].content
    assert await _facts(h, headers) == []


async def test_guests_get_no_memory_tools_or_block(make_harness):
    agent = ScriptedChatModel(script=[AIMessage("네")])
    h = await make_harness(chat_model=agent)
    headers = await h.guest()

    await h.chat(headers, await h.new_conversation(headers), "안녕")

    assert not {"remember", "recall_memory", "forget"} & set(_tool_names(agent))
    assert "<memory>" not in _last_user(agent)


async def test_a_member_who_turned_memory_off_gets_none_of_it(make_harness):
    h, agent, headers, user_id = await _member(make_harness, [AIMessage("네")])
    cid = await h.new_conversation(headers)
    await _seed(h, user_id, cid, "나", "좋아하는 음료", value="라떼")
    await h.client.put("/memory/settings", headers=headers, json={"enabled": False})

    await h.chat(headers, cid, "안녕")

    assert not {"remember", "recall_memory", "forget"} & set(_tool_names(agent))
    assert "<memory>" not in _last_user(agent)


async def test_the_memory_block_stays_when_help_center_sources_arrive(make_harness):
    h, agent, headers, user_id = await _member(make_harness, [AIMessage("하루 20만 토큰이에요.")],
                                               rag_enabled=True)
    cid = await h.new_conversation(headers)
    await _seed(h, user_id, cid, "나", "요금제 관심", value="무료")

    await h.chat(headers, cid, "무료 요금제 하루 토큰 한도가 얼마야?")

    turn = _last_user(agent)
    assert "<sources>" in turn and "<memory>" in turn


@pytest.mark.parametrize(("text", "sensitive"), [
    ("900101-1234567", True),          # resident registration number
    ("4111-1111-1111-1111", True),     # card number (Luhn-valid)
    ("110-123-456789", True),          # bank account format
    ("비밀번호는 hunter2", True),
    ("password: hunter2", True),
    ("라떼", False),
    ("부산 해운대구", False),
    ("1234-5678", False),              # too short to be a card or an account
])
def test_sensitive_values_are_recognised(text, sensitive):
    assert (memory.sensitive_reason(text) is not None) is sensitive


async def test_the_router_sees_the_memory_so_it_does_not_ask_what_it_knows(make_harness):
    # Found live: with the memory only on the answering model's turn, the router took
    # "뭐 주문하면 좋을까?" for ambiguous and asked "어떤 음료를 좋아하시나요?" itself.
    from app.services.chat_models import LangChainProvider
    from tests.fakes import structured

    router = ScriptedChatModel(script=[structured(
        needs_search=False, query="", needs_account=False, needs_past=False,
        needs_clarification=False, clarifying_question="")])
    agent = ScriptedChatModel(script=[AIMessage("라떼 어떠세요?")])
    h = await make_harness(llm=LangChainProvider(router), chat_model=agent)
    r = await h.signup(MEMBER)
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    cid = await h.new_conversation(headers)
    await _seed(h, uuid.UUID(r.json()["user"]["id"]), cid, "나", "좋아하는 음료", value="라떼")

    await h.chat(headers, cid, "카페 왔는데 뭐 주문할까?")

    routed = router.calls[0]["messages"][-1].content
    assert "사용자 → 좋아하는 음료 → 라떼" in routed


async def test_the_member_keeps_their_own_wording_in_their_list(make_harness):
    # "사용자" is only how the model is shown it; the member's list says what they wrote.
    h, agent, headers, user_id = await _member(make_harness, [])
    await _seed(h, user_id, await h.new_conversation(headers), "나", "좋아하는 음료",
                value="라떼")

    assert await _facts(h, headers) == [("나", "좋아하는 음료", "라떼")]
