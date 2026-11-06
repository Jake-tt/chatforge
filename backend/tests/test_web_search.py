"""Web search tool: Tavily behind a provider, a per-user daily cap, and the agent's use of it.

Tavily (the external boundary) is faked: `FakeWeb` for the agent tests, an
`httpx.MockTransport` for the request the provider sends. The graph, quota and Redis
counter are real.
"""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.search import SearchHit
from app.services.web_search import TAVILY_MAX_QUERY_CHARS, TavilySearch
from tests.fakes import tool_call
from tests.test_agent import _agent_harness, _answer, _citations, _route
from tests.test_resilience import DownRedis

WEB_ON = {"web_search_enabled": True, "tavily_api_key": "tvly-test"}
FORECAST = SearchHit("https://weather.example/seoul", "서울 날씨 예보",
                     "https://weather.example/seoul", "내일 서울 맑음, 최고 15도", 0.9)


class FakeWeb:
    def __init__(self, hits: list[SearchHit] | None = None, error: Exception | None = None):
        self._hits = hits if hits is not None else [FORECAST]
        self._error = error
        self.queries: list[str] = []

    async def search(self, query: str, k: int) -> list[SearchHit]:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._hits[:k]


def _tool_result(agent, call: int) -> str:
    return agent.calls[call]["messages"][-1].content


async def test_the_agent_searches_the_web_and_cites_the_result(make_harness):
    web = FakeWeb()
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_web", query="서울 내일 날씨"), _answer("내일 서울은 맑아요 [1].")],
        web_search=web, **WEB_ON)

    result = await h.chat(headers, cid, "내일 서울 날씨 어때?")

    assert web.queries == ["서울 내일 날씨"]
    assert result.text == "내일 서울은 맑아요 [1]."
    assert _citations(result)[0]["url"] == "https://weather.example/seoul"
    # Web pages are material to quote, never instructions.
    assert "<sources>" in _tool_result(agent, 1) and "내일 서울 맑음" in _tool_result(agent, 1)


async def test_web_searches_stop_at_the_daily_limit(make_harness):
    web = FakeWeb()
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route(), _route()],
        agent=[tool_call("search_web", query="날씨"), _answer("맑아요 [1]."),
               tool_call("search_web", query="미세먼지"), _answer("검색 한도를 넘었어요.")],
        web_search=web, guest_daily_web_searches=1, **WEB_ON)

    await h.chat(headers, cid, "날씨 어때?")
    result = await h.chat(headers, cid, "미세먼지는?")

    assert web.queries == ["날씨"]  # the second search never reached Tavily
    assert "daily web search limit" in _tool_result(agent, 3)
    assert result.last("done")["stop_reason"] == "end_turn"


async def test_web_search_is_refused_while_redis_is_down(make_harness):
    # The cap protects a paid quota, so unlike the other rate limits it fails closed.
    web = FakeWeb()
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_web", query="날씨"), _answer("지금은 검색할 수 없어요.")],
        web_search=web, redis=DownRedis(), **WEB_ON)

    result = await h.chat(headers, cid, "날씨 어때?")

    assert web.queries == []
    assert "daily web search limit" in _tool_result(agent, 1)
    assert result.text == "지금은 검색할 수 없어요."


async def test_a_failing_web_search_is_reported_to_the_model(make_harness):
    web = FakeWeb(error=httpx.ConnectError("tavily unreachable"))
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[_route()],
        agent=[tool_call("search_web", query="날씨"), _answer("검색에 실패했어요.")],
        web_search=web, **WEB_ON)

    result = await h.chat(headers, cid, "날씨 어때?")

    assert _tool_result(agent, 1).startswith("Error:")
    assert result.text == "검색에 실패했어요."


@pytest.mark.parametrize(("enabled", "offered"), [(True, True), (False, False)])
async def test_web_search_is_offered_only_when_enabled(make_harness, enabled, offered):
    settings = WEB_ON if enabled else {}
    h, agent, headers, cid = await _agent_harness(
        make_harness, routes=[ConnectionError("router down")], agent=[_answer("답")],
        web_search=FakeWeb(), **settings)

    await h.chat(headers, cid, "안녕")

    names = [t["function"]["name"] for t in agent.calls[0]["tools"]]
    assert ("search_web" in names) is offered


def test_enabling_web_search_needs_an_api_key():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_search_enabled=True)


async def test_tavily_request_and_result_mapping():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "서울 날씨", "url": "https://w.example/1", "content": "맑음", "score": 0.8},
            {"title": "", "url": "https://w.example/2", "content": "", "score": 0.1},
        ]})

    tavily = TavilySearch("tvly-test", timeout_seconds=5, transport=httpx.MockTransport(handler))
    hits = await tavily.search("가" * 500, 3)

    request = sent[0]
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["Authorization"] == "Bearer tvly-test"
    assert json.loads(request.content) == {
        "query": "가" * TAVILY_MAX_QUERY_CHARS, "max_results": 3, "search_depth": "basic"}
    # A result without text has nothing to cite.
    assert hits == [
        SearchHit("https://w.example/1", "서울 날씨", "https://w.example/1", "맑음", 0.8)]


async def test_a_tavily_error_status_raises():
    tavily = TavilySearch("tvly-test", timeout_seconds=5, transport=httpx.MockTransport(
        lambda request: httpx.Response(432, json={"detail": "plan limit exceeded"})))

    with pytest.raises(httpx.HTTPStatusError):
        await tavily.search("날씨", 3)
