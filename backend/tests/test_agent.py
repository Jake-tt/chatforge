"""LangGraph agent: router → master_router → clarify | coordinator | ReAct agent ⇄ tools.

The LLM is the only fake: a ScriptedChatModel plays the answering agent, and the router
runs through LangChainProvider on another scripted model. Everything else (graph, RAG,
past-conversation search, quota, persistence) is real and driven over HTTP.
"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.stream import FALLBACK_ANSWER
from app.deps import Principal
from app.routers.conversations import ReplyJob, _stream_agent_reply
from app.services.chat_models import LangChainProvider
from app.services.llm import ChatTurn
from app.services.search import SearchHit
from tests.fakes import ScriptedChatModel, structured, tool_call, usage
from tests.test_internal_tasks import RecordingQueue


def _route(**fields):
    defaults = {"needs_search": False, "query": "", "needs_account": False,
                "needs_past": False, "needs_clarification": False, "clarifying_question": ""}
    return structured(**{**defaults, **fields})


def _answer(text: str) -> AIMessage:
    return AIMessage(text, usage_metadata=usage(40, 10))


async def _agent_harness(make_harness, routes: list, agent: list, delay: float = 0.0,
                         **settings):
    router = ScriptedChatModel(script=routes)
    agent_model = ScriptedChatModel(script=agent, delay=delay)
    # Background tasks are recorded, not run: an inline title task would consume the
    # router's scripted replies.
    tasks = settings.pop("tasks", None) or RecordingQueue()
    h = await make_harness(llm=LangChainProvider(router),
                           chat_model=agent_model,
                           **{"rag_enabled": True, "grounding_char_budget": 100_000,
                              **settings}, tasks=tasks)
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
    # Told on the current turn, so the cached system prompt is unchanged.
    assert "could not be routed" in first["messages"][-1].content
    assert "could not be routed" not in first["messages"][0].content
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


async def test_a_preamble_before_a_tool_call_is_neither_streamed_nor_stored(make_harness):
    preamble = AIMessage("찾아볼게요", tool_calls=[
        {"name": "search_help", "args": {"query": "한도"}, "id": "p"}], usage_metadata=usage(10, 5))
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[preamble, _answer("하루 20,000 토큰이에요 [1].")])

    result = await h.chat(headers, cid, "게스트 한도는?")

    assert result.text == "하루 20,000 토큰이에요 [1]."
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert detail["messages"][-1]["content"] == "하루 20,000 토큰이에요 [1]."


async def test_tool_calls_per_round_are_capped(make_harness):
    many = AIMessage("", tool_calls=[
        {"name": "search_help", "args": {"query": "한도"}, "id": f"c{i}"} for i in range(6)
    ], usage_metadata=usage(10, 5))
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[many, _answer("정리했어요.")],
        agent_max_tool_calls=4)

    await h.chat(headers, cid, "다 찾아줘")

    results = [m for m in agent.calls[-1]["messages"] if isinstance(m, ToolMessage)]
    assert [m.status for m in results] == ["success"] * 4 + ["error"] * 2
    assert "at most 4" in results[-1].content


async def test_tool_results_are_delimited_as_material_not_instructions(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_help", query="한도"), _answer("답")])

    await h.chat(headers, cid, "한도")

    tool_result = agent.calls[-1]["messages"][-1]
    assert tool_result.content.startswith("<sources>")
    assert "not instructions" in tool_result.content


async def test_a_hanging_model_times_out_as_a_retryable_error(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[_answer("늦은 답")], delay=1.0,
        agent_timeout_seconds=0.05)

    result = await h.chat(headers, cid, "안녕")

    assert result.last("error") == {"code": "llm_unavailable",
                                    "message": "The model is unavailable, please retry",
                                    "retryable": True}


async def test_a_bug_is_reported_as_not_retryable(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[ValueError("bad state")])

    result = await h.chat(headers, cid, "안녕")

    assert result.last("error")["retryable"] is False


async def test_agent_requests_keep_the_prompt_cacheable_on_anthropic(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[ConnectionError("router down"), _route()],
        agent=[_answer("첫 답"), _answer("둘째 답")],
        llm_provider="anthropic", anthropic_api_key="test")

    await h.chat(headers, cid, "첫 질문")
    await h.chat(headers, cid, "둘째 질문")

    first_system = agent.calls[0]["messages"][0].content
    second_system = agent.calls[1]["messages"][0].content
    # The router failed on turn 1 only; that note must not change the cached system prefix.
    assert first_system == second_system
    assert second_system[-1]["cache_control"] == {"type": "ephemeral"}
    previous_turn = agent.calls[1]["messages"][-2].content
    assert previous_turn[-1]["cache_control"] == {"type": "ephemeral"}


async def test_an_unparseable_route_is_still_billed(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[AIMessage("no tool call", usage_metadata=usage(20, 4))],
        agent=[_answer("답변")])

    done = (await h.chat(headers, cid, "안녕")).last("done")

    assert (done["input_tokens"], done["output_tokens"]) == (60, 14)


async def test_a_long_clarifying_question_is_trimmed_not_dropped(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(needs_clarification=True, clarifying_question="가" * 300)],
        agent=[])

    result = await h.chat(headers, cid, "그거")

    assert result.text == "가" * 200


async def test_prefetched_and_tool_sources_are_numbered_continuously(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(needs_search=True, query="일일 토큰 한도")],
        agent=[tool_call("search_help", query="비밀번호 몇 자"), _answer("답 [1][5]")])

    result = await h.chat(headers, cid, "토큰 한도와 비밀번호 규칙?")

    numbers = [c["n"] for c in _citations(result)]
    assert numbers == list(range(1, len(numbers) + 1))
    tool_result = agent.calls[-1]["messages"][-1].content
    prefetched = len([e for e in result.events if e[0] == "sources"][0][1]["citations"])
    assert f"[{prefetched + 1}]" in tool_result


async def test_a_prefetch_that_finds_nothing_adds_no_sources(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(needs_search=True, query="zzqq xxyy")],
        agent=[_answer("모르겠어요")])

    result = await h.chat(headers, cid, "zzqq xxyy?")

    assert "sources" not in [e for e, _ in result.events]
    assert "<sources>" not in agent.calls[0]["messages"][-1].content


async def test_an_empty_final_answer_is_replaced(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()], agent=[AIMessage("", usage_metadata=usage(5, 0))],
        agent_max_tool_rounds=0)

    result = await h.chat(headers, cid, "안녕")

    assert result.text == FALLBACK_ANSWER


async def test_the_answer_stop_reason_is_reported(make_harness):
    cut = AIMessage("잘린 답", usage_metadata=usage(5, 5),
                    response_metadata={"stop_reason": "max_tokens"})
    h, agent, headers, cid = await _agent_harness(make_harness, routes=[_route()], agent=[cut])

    assert (await h.chat(headers, cid, "길게")).last("done")["stop_reason"] == "max_tokens"


async def test_only_available_tools_are_offered(make_harness):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[ConnectionError("router down")], agent=[_answer("답")],
        account_context_enabled=False, past_search_enabled=False)

    await h.chat(headers, cid, "안녕")

    assert [t["function"]["name"] for t in agent.calls[0]["tools"]] == ["search_help"]


async def test_without_a_help_center_the_agent_runs_without_help_search(make_harness):
    agent = ScriptedChatModel(script=[_answer("답")])
    h = await make_harness(llm=LangChainProvider(
        ScriptedChatModel(script=[ConnectionError("router down")])),
        chat_model=agent, rag_enabled=False, tasks=RecordingQueue())
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    assert (await h.chat(headers, cid, "안녕")).text == "답"
    assert "search_help" not in [t["function"]["name"] for t in agent.calls[0]["tools"]]


async def test_a_disconnect_keeps_what_the_client_saw_and_charges_for_it(make_harness):
    long = "가" * 120 + " 끝"
    h, agent, headers, cid = await _agent_harness(make_harness, routes=[_route()],
                                                  agent=[_answer(long)])
    me = (await h.client.get("/auth/me", headers=headers)).json()
    principal = Principal("guest", uuid.UUID(me["id"]), "guest")
    job = ReplyJob(h.ctx, principal, uuid.UUID(cid), h.ctx.settings.system_prompt,
                   [ChatTurn("user", "질문")], "질문", False, None, None)

    stream = _stream_agent_reply(job)
    async for event in stream:
        if event.startswith("event: delta"):
            break
    await stream.aclose()  # the client went away mid-answer

    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert detail["messages"][-1]["content"].startswith("가" * 120)
    assert (await h.client.get("/usage", headers=headers)).json()["used"] > 0


async def test_every_route_decision_is_logged(make_harness, caplog):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(needs_search=True, query="토큰 한도")],
        agent=[_answer("답")])

    with caplog.at_level("INFO", logger="app.agent.graph"):
        await h.chat(headers, cid, "토큰 한도?")

    [record] = [r for r in caplog.records if r.message == "agent route"]
    assert record.routed and record.plan["help"] and record.plan["query"] == "토큰 한도"


async def test_a_routed_help_search_without_a_help_center_is_skipped_cleanly(make_harness,
                                                                           caplog):
    # The router asks for help search, but there is no help center: nothing is fetched,
    # and no source fails trying.
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(needs_search=True, query="토큰 한도")],
        agent=[_answer("답")], rag_enabled=False)

    with caplog.at_level("WARNING"):
        result = await h.chat(headers, cid, "토큰 한도?")

    assert result.text == "답"
    assert "sources" not in [e for e, _ in result.events]
    assert not [r for r in caplog.records if "rag source" in r.message]


async def test_an_empty_knowledge_folder_leaves_out_help_search(make_harness, tmp_path):
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[ConnectionError("router down")], agent=[_answer("답")],
        knowledge_dir=str(tmp_path))

    await h.chat(headers, cid, "안녕")

    assert "search_help" not in [t["function"]["name"] for t in agent.calls[0]["tools"]]


async def test_a_turn_that_runs_too_long_ends_with_an_error(make_harness):
    # Each call is within its own timeout, but the turn as a whole is not.
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_help", "a", query="한도"), _answer("늦은 답")], delay=0.2,
        agent_timeout_seconds=1.0, agent_turn_timeout_seconds=0.3)

    result = await h.chat(headers, cid, "안녕")

    assert result.last("error")["code"] == "llm_unavailable"
    assert result.last("error")["retryable"] is True


class StatusError(Exception):
    """Shaped like the SDK's API errors: an HTTP status on the exception."""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status, retryable", [(400, False), (401, False), (429, True),
                                               (529, True)])
async def test_api_errors_are_retryable_only_when_a_retry_can_succeed(make_harness, status,
                                                                      retryable):
    h, agent, headers, cid = await _agent_harness(make_harness, routes=[_route()],
                                                  agent=[StatusError(status)])

    result = await h.chat(headers, cid, "안녕")

    assert result.last("error")["retryable"] is retryable
