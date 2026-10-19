"""Personal context in RAG: the user's own account and earlier conversations."""

from tests.test_rag import BrokenSearch, SpyProvider

ACCOUNT_QUESTION = "오늘 남은 토큰 한도는?"


async def _harness(make_harness, llm=None, **settings):
    llm = llm or SpyProvider()
    h = await make_harness(llm=llm, rag_enabled=True, **settings)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    return h, llm, headers, cid


def _titles(result) -> list[str]:
    return [c["title"] for c in result.last("sources")["citations"]]


async def test_account_facts_and_help_articles_are_both_cited(make_harness):
    h, llm, headers, cid = await _harness(make_harness)

    result = await h.chat(headers, cid, ACCOUNT_QUESTION)

    titles = _titles(result)
    assert titles[0] == "내 계정 현황"
    assert "요금제와 사용 한도 › 일일 토큰 한도" in titles
    context = llm.contexts[-1]
    assert "[1] 내 계정 현황" in context and "요금제: 게스트" in context
    assert "0 / 20,000 토큰" in context


async def test_account_snapshot_says_when_it_was_taken(make_harness):
    # The citation is stored with the answer; reopened later it must not read as current.
    h, _, headers, cid = await _harness(make_harness)

    await h.chat(headers, cid, ACCOUNT_QUESTION)

    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert "기준 시각:" in detail["messages"][-1]["citations"][0]["snippet"]


async def test_account_facts_are_added_only_when_the_router_asks(make_harness):
    h, _, headers, cid = await _harness(make_harness)

    general = await h.chat(headers, cid, "무료 요금제 하루 토큰 한도가 얼마야?")
    # "안내 " contains "내 " but is not about the user's own account.
    signup = await h.chat(headers, cid, "가입 안내 해줄래?")

    assert "내 계정 현황" not in _titles(general)
    assert "내 계정 현황" not in _titles(signup)


async def test_account_facts_survive_a_help_search_outage(make_harness):
    h, _, headers, cid = await _harness(make_harness, search=BrokenSearch())

    result = await h.chat(headers, cid, ACCOUNT_QUESTION)

    assert _titles(result) == ["내 계정 현황"]


async def test_compression_never_touches_account_facts(make_harness):
    # Summaries may reword numbers or drop a source; the user's own figures are sent as-is.
    llm = SpyProvider(summary="토큰 한도는 요금제마다 다릅니다 [2].")
    h, _, headers, cid = await _harness(make_harness, llm=llm, grounding_char_budget=50)

    await h.chat(headers, cid, ACCOUNT_QUESTION)

    context = llm.contexts[-1]
    assert "0 / 20,000 토큰" in context
    assert "토큰 한도는 요금제마다 다릅니다 [2]." in context
    assert all("요금제: 게스트" not in prompt for prompt in llm.compress_prompts)


async def test_a_summary_citing_the_account_number_is_rejected(make_harness):
    # [1] is the account snapshot, which the summarizer never saw.
    llm = SpyProvider(summary="남은 한도는 많습니다 [1].")
    h, _, headers, cid = await _harness(make_harness, llm=llm, grounding_char_budget=50)

    await h.chat(headers, cid, ACCOUNT_QUESTION)

    assert "남은 한도는 많습니다" not in llm.contexts[-1]


async def test_account_context_can_be_switched_off(make_harness):
    h, _, headers, cid = await _harness(make_harness, account_context_enabled=False)

    result = await h.chat(headers, cid, ACCOUNT_QUESTION)

    assert "내 계정 현황" not in _titles(result)
