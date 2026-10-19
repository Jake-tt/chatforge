"""Help-center RAG: routing, hybrid search, citations and their fallbacks."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import Settings
from app.services.llm import ChatTurn, FakeProvider, LLMError, StreamEvent, TextResult, Usage
from app.services.search import BM25Search, load_markdown_corpus, tokenize

KNOWLEDGE = Path(Settings().knowledge_dir)


def test_korean_is_indexed_as_bigrams():
    assert tokenize("토큰한도를 GPT 2개") == ["토큰", "큰한", "한도", "도를", "gpt", "2", "개"]


async def test_bm25_finds_the_right_help_section():
    search = BM25Search(load_markdown_corpus(KNOWLEDGE))

    hits = await search.search("무료 요금제 하루 토큰 한도", limit=3)
    assert hits[0].doc_id == "plans#2"

    hits = await search.search("비밀번호 몇 자 이상", limit=3)
    assert hits[0].doc_id == "account#3"

    assert await search.search("오늘 날씨 어때", limit=3) == []


class SpyProvider(FakeProvider):
    """FakeProvider that records what it was sent and lets tests script the aux calls."""

    def __init__(self, summary: str | None = None, router_delay: float = 0.0,
                 router_error: bool = False):
        super().__init__()
        self.systems: list[str] = []
        self.calls: list[list[ChatTurn]] = []
        self._summary = summary
        self._router_delay = router_delay
        self._router_error = router_error

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        self.systems.append(system)
        self.calls.append(turns)
        async for event in super().stream(system, turns):
            yield event

    async def structured(self, system, prompt, schema):
        if self._router_error:
            raise LLMError("router down", retryable=True)
        await asyncio.sleep(self._router_delay)
        return await super().structured(system, prompt, schema)

    @property
    def contexts(self) -> list[str]:
        """Grounding sent with the current user turn of each call ("" when none)."""
        return [turns[-1].context for turns in self.calls]

    async def complete(self, system: str, prompt: str) -> TextResult:
        if self._summary is None:
            return await super().complete(system, prompt)
        return TextResult(self._summary, Usage(100, 20))


async def _rag_harness(make_harness, llm=None, **settings):
    llm = llm or SpyProvider()
    h = await make_harness(llm=llm, rag_enabled=True, **settings)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    return h, llm, headers, cid


async def test_knowledge_question_is_grounded_and_cited(make_harness):
    h, llm, headers, cid = await _rag_harness(make_harness)

    result = await h.chat(headers, cid, "무료 요금제 하루 토큰 한도가 얼마야?")

    sources = result.last("sources")["citations"]
    assert sources[0]["n"] == 1
    assert sources[0]["title"] == "요금제와 사용 한도 › 일일 토큰 한도"
    assert "<sources>" in llm.contexts[-1] and "[1] 요금제와 사용 한도" in llm.contexts[-1]
    # Citations are stored with the answer, and routing tokens are charged with it.
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert detail["messages"][-1]["citations"] == sources
    done = result.last("done")
    usage = (await h.client.get("/usage", headers=headers)).json()
    assert usage["used"] == done["billed_tokens"]


async def test_sources_ride_on_the_current_turn_and_the_system_prompt_stays_frozen(
    make_harness,
):
    # Sources differ every turn. In the system prompt they would change the start of the
    # prompt and void the cached conversation behind it, so they go with the current turn.
    h, llm, headers, cid = await _rag_harness(make_harness)

    await h.chat(headers, cid, "무료 요금제 하루 토큰 한도가 얼마야?")
    await h.chat(headers, cid, "비밀번호는 몇 자 이상이어야 해?")

    assert llm.systems == [h.ctx.settings.system_prompt] * 2
    first, second = llm.calls
    assert "<sources>" in first[-1].context and "<sources>" in second[-1].context
    # History is replayed as stored: earlier turns carry no sources.
    assert [t.context for t in second[:-1]] == ["", ""]
    assert second[0].content == "무료 요금제 하루 토큰 한도가 얼마야?"


async def test_small_talk_skips_search(make_harness):
    h, llm, headers, cid = await _rag_harness(make_harness)

    result = await h.chat(headers, cid, "안녕 반가워")

    assert not any(e == "sources" for e, _ in result.events)
    assert "<sources>" not in llm.contexts[-1]


async def test_router_timeout_answers_without_search(make_harness):
    llm = SpyProvider(router_delay=1.0)
    h, _, headers, cid = await _rag_harness(make_harness, llm=llm, router_timeout_seconds=0.05)

    result = await h.chat(headers, cid, "토큰 한도가 얼마야?")

    assert result.response.status_code == 200
    assert result.text.startswith("(fake)")
    assert "<sources>" not in llm.contexts[-1]


async def test_router_failure_answers_without_search(make_harness):
    llm = SpyProvider(router_error=True)
    h, _, headers, cid = await _rag_harness(make_harness, llm=llm)

    result = await h.chat(headers, cid, "토큰 한도가 얼마야?")

    assert result.last("done")["stop_reason"] == "end_turn"
    assert "<sources>" not in llm.contexts[-1]


async def test_hybrid_search_kicks_in_on_later_turns(make_harness):
    h, llm, headers, cid = await _rag_harness(make_harness, search_after_turns=3)

    # No question mark or keyword: the (fake) router says no search…
    statement = "대화 제목은 자동으로 붙어"
    first = await h.chat(headers, cid, statement)
    await h.chat(headers, cid, "음")
    # …but from the third user turn on, any non-empty query is searched.
    third = await h.chat(headers, cid, statement)

    assert not any(e == "sources" for e, _ in first.events)
    assert third.last("sources")["citations"][0]["title"] == "채팅 기능 › 대화 제목"


async def test_long_sources_are_compressed_when_citations_survive(make_harness):
    llm = SpyProvider(summary="무료 요금제는 하루 200,000 토큰입니다 [1].")
    h, _, headers, cid = await _rag_harness(make_harness, llm=llm, grounding_char_budget=50)

    await h.chat(headers, cid, "무료 요금제 토큰 한도?")

    assert "무료 요금제는 하루 200,000 토큰입니다 [1]." in llm.contexts[-1]


async def test_summary_that_drops_citations_is_discarded(make_harness):
    llm = SpyProvider(summary="무료 요금제는 하루 200,000 토큰입니다.")  # no [n]
    h, _, headers, cid = await _rag_harness(make_harness, llm=llm, grounding_char_budget=200)

    result = await h.chat(headers, cid, "무료 요금제 토큰 한도?")

    context = llm.contexts[-1]
    assert "하루 200,000 토큰입니다." not in context
    assert "[1] 요금제와 사용 한도" in context and "…" in context  # truncated raw sources
    assert result.last("sources")["citations"]  # attribution kept either way


class BrokenSearch:
    async def search(self, query: str, limit: int):
        raise ConnectionError("search backend down")


async def test_search_outage_answers_without_sources(make_harness):
    llm = SpyProvider()
    h = await make_harness(llm=llm, rag_enabled=True, search=BrokenSearch())
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    result = await h.chat(headers, cid, "토큰 한도가 얼마야?")

    assert result.response.status_code == 200
    assert not any(e == "sources" for e, _ in result.events)
