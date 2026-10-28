"""LangGraph agent: router → master_router → clarify | coordinator | ReAct agent ⇄ tools.

The LLM is the only fake: a ScriptedChatModel plays the answering agent, and the router
runs through LangChainProvider on another scripted model. Everything else (graph, RAG,
past-conversation search, quota, persistence) is real and driven over HTTP.
"""

import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from app.services.chat_models import EchoChatModel, LangChainProvider
from app.services.search import SearchHit
from tests.fakes import ScriptedChatModel, structured, tool_call, usage
from tests.test_internal_tasks import RecordingQueue


def _route(**fields):
    defaults = {"needs_search": False, "query": "", "needs_account": False,
                "needs_past": False, "needs_clarification": False, "clarifying_question": ""}
    return structured(**{**defaults, **fields})


def _answer(text: str) -> AIMessage:
    return AIMessage(text, usage_metadata=usage(40, 10))


async def _agent_harness(make_harness, routes: list, agent: list, **settings):
    router = ScriptedChatModel(script=routes)
    agent_model = ScriptedChatModel(script=agent)
    # Background tasks are recorded, not run: an inline title task would consume the
    # router's scripted replies.
    tasks = settings.pop("tasks", None) or RecordingQueue()
    h = await make_harness(llm=LangChainProvider(main=EchoChatModel(), aux=router),
                           chat_model=agent_model, agent_enabled=True, rag_enabled=True,
                           grounding_char_budget=100_000, tasks=tasks, **settings)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    return h, agent_model, headers, cid


def _citations(result) -> list[dict]:
    sources = [d for e, d in result.events if e == "sources"]
    return sources[-1]["citations"] if sources else []


async def test_routed_help_question_is_grounded_by_the_coordinator(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness,
        routes=[_route(needs_search=True, query="무료 요금제 하루 토큰 한도")],
        agent=[_answer("무료 요금제는 하루 200,000 토큰입니다 [1].")],
    )

    result = await h.chat(headers, cid, "무료 요금제 하루 토큰 한도가 얼마야?")

    assert result.text == "무료 요금제는 하루 200,000 토큰입니다 [1]."
    assert _citations(result)[0]["title"] == "요금제와 사용 한도 › 일일 토큰 한도"
    current_turn = agent.calls[0]["messages"][-1]
    assert "<sources>" in current_turn.content and current_turn.content.endswith("얼마야?")


async def test_agent_calls_a_tool_and_answers_from_its_result(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness,
        routes=[_route(), _route()],
        agent=[_answer("기억해 둘게요"),
               tool_call("search_past_conversations", query="부산 여행 숙소"),
               _answer("해운대 근처로 정하셨어요 [1].")],
    )
    await h.chat(headers, cid, "부산 여행 숙소는 해운대 근처로 정했어")
    now = await h.new_conversation(headers)

    result = await h.chat(headers, now, "숙소 어디로 했더라")

    assert result.text == "해운대 근처로 정하셨어요 [1]."
    tool_result = agent.calls[-1]["messages"][-1]
    assert isinstance(tool_result, ToolMessage) and "해운대" in tool_result.content
    assert _citations(result)[0]["url"] == f"/conversations/{cid}"


class SlowSearch:
    async def search(self, query: str, limit: int) -> list[SearchHit]:
        await asyncio.sleep(0.3)
        return [SearchHit("help#1", "도움말", "/help", f"{query} 안내", 1.0)]


async def test_tool_calls_in_one_message_run_concurrently(make_harness):
    calls = AIMessage("", tool_calls=[
        {"name": "search_help", "args": {"query": "한도"}, "id": "a"},
        {"name": "search_help", "args": {"query": "요금제"}, "id": "b"},
    ], usage_metadata=usage(10, 5))
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[calls, _answer("둘 다 확인했어요 [1][2].")],
        search=SlowSearch(),
    )
    started = asyncio.get_running_loop().time()

    result = await h.chat(headers, cid, "한도와 요금제 둘 다 알려줘")

    assert asyncio.get_running_loop().time() - started < 0.55  # 0.3s each, not 0.6s
    assert [c["n"] for c in _citations(result)] == [1, 2]
    results = [m for m in agent.calls[-1]["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in results] == ["a", "b"]


async def test_router_failure_hands_the_choice_of_tools_to_the_agent(make_harness):
    # Routing is an optimisation; when it fails the agent decides for itself instead of
    # answering blind.
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[ConnectionError("router down")],
        agent=[tool_call("get_account_usage"), _answer("오늘 0 토큰을 쓰셨어요 [1].")],
    )

    result = await h.chat(headers, cid, "오늘 얼마나 썼어?")

    assert result.text == "오늘 0 토큰을 쓰셨어요 [1]."
    first = agent.calls[0]
    assert {t["function"]["name"] for t in first["tools"]} == {
        "search_help", "search_past_conversations", "get_account_usage"}
    assert "could not be routed" in first["messages"][0].content
    assert _citations(result)[0]["title"] == "내 계정 현황"


async def test_a_clarifying_question_ends_the_turn_without_summary_or_agent(make_harness):
    queue = RecordingQueue()
    h, agent, headers, cid = await _agent_harness(
        make_harness,
        routes=[_route(), _route(), _route(needs_clarification=True,
                                          clarifying_question="어떤 요금제를 말씀하시나요?")],
        agent=[_answer("네"), _answer("네")],
        summary_enabled=True, summary_trigger_messages=2, task_backend="cloudtasks",
        tasks=queue,
    )
    await h.chat(headers, cid, "안녕")
    await h.chat(headers, cid, "질문 있어")
    queue.enqueued.clear()

    result = await h.chat(headers, cid, "그거 바꾸고 싶어")

    assert result.text == "어떤 요금제를 말씀하시나요?"
    assert len(agent.calls) == 2  # the agent never ran for this turn
    # Summarizing now would bury the question the user is about to answer.
    assert "summarize_conversation" not in [name for name, _ in queue.enqueued]
    assert "suggestions" not in [e for e, _ in result.events]


async def test_a_malformed_tool_call_is_retried_once_without_tools(make_harness):
    malformed = AIMessage("", invalid_tool_calls=[
        {"name": "search_help", "args": "not json at all", "id": "x", "error": "bad json"}],
        usage_metadata=usage(10, 5))
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[malformed, _answer("바로 답할게요.")])

    result = await h.chat(headers, cid, "아무거나")

    assert result.text == "바로 답할게요."
    assert agent.calls[1]["tools"] == []


async def test_tool_rounds_are_capped_and_the_last_call_must_answer(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_help", "c1", query="a"), tool_call("search_help", "c2",
                                                                     query="b"),
               _answer("여기까지 찾은 내용이에요.")],
        agent_max_tool_rounds=2,
    )

    result = await h.chat(headers, cid, "계속 찾아줘")

    assert result.text == "여기까지 찾은 내용이에요."
    assert [bool(c["tools"]) for c in agent.calls] == [True, True, False]


async def test_router_and_every_agent_round_are_billed(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_help", query="한도"), _answer("답변")],
    )

    done = (await h.chat(headers, cid, "한도 알려줘")).last("done")

    # router 20 in / 4 out, tool round 10/5, answer 40/10; output weighted x5.
    assert (done["input_tokens"], done["output_tokens"]) == (70, 19)
    assert done["billed_tokens"] == 70 + 19 * 5
    assert (await h.client.get("/usage", headers=headers)).json()["used"] == 165


async def test_agent_model_failure_streams_an_error_and_keeps_the_conversation(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[ConnectionError("model server down")])

    result = await h.chat(headers, cid, "안녕")

    assert result.last("error")["code"] == "llm_unavailable"
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert [m["role"] for m in detail["messages"]] == ["user"]


async def test_a_tool_call_with_bad_arguments_gets_an_error_back_not_a_failed_turn(make_harness):
    # LangChain's lenient parser can turn truncated args into `{}`: the call is "valid" but
    # misses `query`. The model is told, and can try again.
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_help"), _answer("검색어 없이 답할게요.")])

    result = await h.chat(headers, cid, "찾아줘")

    assert result.text == "검색어 없이 답할게요."
    tool_result = agent.calls[1]["messages"][-1]
    assert isinstance(tool_result, ToolMessage) and tool_result.status == "error"
