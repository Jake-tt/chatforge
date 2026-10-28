"""Follow-up question chips: validation of model output and graceful fallback."""

import asyncio

from langchain_core.messages import AIMessage

from app.services.llm import FakeProvider, LLMError
from app.services.suggestions import FollowUps
from tests.fakes import ScriptedChatModel, usage


def test_validator_cleans_model_artefacts():
    parsed = FollowUps.model_validate({"questions": [
        "<ctrl46>프로 요금제는 얼마야?",   # tokenizer artefact
        "  프로 요금제는 얼마야? ",          # duplicate after cleanup
        '{"questions": ["x"]}',            # JSON echoed into a string
        "",                                 # empty
        "가" * 61,                          # too long for a chip
        "한도는​ 언제 초기화돼?",       # zero-width space
        42,                                 # wrong type
        "가입하면 뭐가 좋아?",
        "네 번째 질문",                      # over the limit of 3
    ]})
    assert parsed.questions == [
        "프로 요금제는 얼마야?", "한도는 언제 초기화돼?", "가입하면 뭐가 좋아?",
    ]


def test_validator_tolerates_wrong_shape():
    assert FollowUps.model_validate({"questions": "not a list"}).questions == []


async def test_suggestions_are_streamed_before_done_and_charged(make_harness):
    h = await make_harness(suggestions_enabled=True)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    result = await h.chat(headers, cid, "요금제 알려줘")

    names = [e for e, _ in result.events]
    assert names.index("suggestions") < names.index("done")
    assert result.last("suggestions")["questions"] == ["더 자세히 설명해줘", "예시를 들어줄래?"]
    done = result.last("done")
    used = (await h.client.get("/usage", headers=headers)).json()["used"]
    assert used == done["billed_tokens"]


class SlowSuggestions(FakeProvider):
    async def structured(self, system, prompt, schema):
        if schema is FollowUps:  # only the suggestion call; the router stays fast
            await asyncio.sleep(1)
        return await super().structured(system, prompt, schema)


class BrokenSuggestions(FakeProvider):
    async def structured(self, system, prompt, schema):
        if schema is FollowUps:
            raise LLMError("bad output", retryable=False)
        return await super().structured(system, prompt, schema)


async def test_slow_or_failing_suggestions_never_break_the_reply(make_harness):
    for llm in (SlowSuggestions(), BrokenSuggestions()):
        h = await make_harness(llm=llm, suggestions_enabled=True,
                               suggestions_timeout_seconds=0.05)
        headers = await h.guest()
        cid = await h.new_conversation(headers)

        result = await h.chat(headers, cid, "요금제 알려줘")

        assert result.last("done")["stop_reason"] == "end_turn"
        assert "suggestions" not in [e for e, _ in result.events]


async def test_no_suggestions_after_a_refusal(make_harness):
    refusal = AIMessage("I can't help with that.", usage_metadata=usage(5, 5),
                        response_metadata={"stop_reason": "refusal"})
    h = await make_harness(chat_model=ScriptedChatModel(script=[refusal]),
                           suggestions_enabled=True)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    result = await h.chat(headers, cid, "something disallowed")

    assert "suggestions" not in [e for e, _ in result.events]
