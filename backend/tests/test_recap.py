"""Recap: what a user sees when they come back to a conversation after a while.

The LLM is the only fake (a provider that records recap prompts); the route, storage and
quota are real and driven over HTTP.
"""

from app.services.llm import FakeProvider, LLMError, Usage
from app.services.recap import RecapOut

IDLE_NOW = {"recap_after_seconds": 0}  # every conversation counts as one left a while ago


class RecordingRecaps(FakeProvider):
    def __init__(self, result: dict | None = None, error: bool = False):
        self.prompts: list[str] = []
        self._result = result
        self._error = error

    async def structured(self, system, prompt, schema):
        if schema is not RecapOut:
            return await super().structured(system, prompt, schema)
        self.prompts.append(prompt)
        if self._error:
            raise LLMError("model down", retryable=True)
        if self._result is not None:
            return RecapOut.model_validate(self._result), Usage(300, 60)
        return await super().structured(system, prompt, schema)


async def _conversation(make_harness, *messages: str, llm=None, **settings):
    h = await make_harness(llm=llm or RecordingRecaps(), **{**IDLE_NOW, **settings})
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    for message in messages:
        await h.chat(headers, cid, message)
    return h, headers, cid


async def _recap(h, headers, cid):
    return await h.client.get(f"/conversations/{cid}/recap", headers=headers)


async def test_coming_back_to_a_conversation_shows_a_recap(make_harness):
    llm = RecordingRecaps({"recap": "부산 숙소를 찾고 있었어요.",
                           "questions": ["해운대 근처 숙소 더 알려줘"]})
    h, headers, cid = await _conversation(make_harness, "부산 여행 계획 중이야", "숙소 추천해줘",
                                          llm=llm)

    r = await _recap(h, headers, cid)

    assert r.status_code == 200
    assert r.json() == {"recap": "부산 숙소를 찾고 있었어요.",
                        "questions": ["해운대 근처 숙소 더 알려줘"], "through_seq": 4}
    assert "부산 여행 계획 중이야" in llm.prompts[0] and "숙소 추천해줘" in llm.prompts[0]


async def test_a_recap_is_reused_until_the_conversation_moves_on(make_harness):
    llm = RecordingRecaps()
    h, headers, cid = await _conversation(make_harness, "one", "two", llm=llm)

    first = await _recap(h, headers, cid)
    again = await _recap(h, headers, cid)
    assert len(llm.prompts) == 1 and again.json() == first.json()

    await h.chat(headers, cid, "three")
    later = await _recap(h, headers, cid)
    assert len(llm.prompts) == 2 and later.json()["through_seq"] == 6


async def test_no_recap_right_after_the_last_message(make_harness):
    llm = RecordingRecaps()
    h, headers, cid = await _conversation(make_harness, "one", "two", llm=llm,
                                          recap_after_seconds=3600)

    assert (await _recap(h, headers, cid)).status_code == 204
    assert llm.prompts == []


async def test_no_recap_for_a_conversation_short_enough_to_read(make_harness):
    llm = RecordingRecaps()
    h, headers, cid = await _conversation(make_harness, "one", llm=llm)

    assert (await _recap(h, headers, cid)).status_code == 204
    assert llm.prompts == []


async def test_a_recap_is_not_charged_to_the_user(make_harness):
    # The user didn't ask for it: like idle compaction, the service pays.
    llm = RecordingRecaps({"recap": "요약", "questions": []})
    h, headers, cid = await _conversation(make_harness, "one", "two", llm=llm)
    used = (await h.client.get("/usage", headers=headers)).json()["used"]

    assert (await _recap(h, headers, cid)).status_code == 200

    assert (await h.client.get("/usage", headers=headers)).json()["used"] == used


async def test_a_failed_recap_shows_nothing_and_is_tried_again_later(make_harness):
    llm = RecordingRecaps(error=True)
    h, headers, cid = await _conversation(make_harness, "one", "two", llm=llm)

    assert (await _recap(h, headers, cid)).status_code == 204
    assert (await _recap(h, headers, cid)).status_code == 204
    assert len(llm.prompts) == 2  # nothing was stored, so the next visit tries again


async def test_recap_output_is_cleaned_before_it_is_shown(make_harness):
    llm = RecordingRecaps({"recap": "  " + "가" * 500,
                           "questions": ["<ctrl46>다음 질문", '{"x": 1}', "다음 질문"]})
    h, headers, cid = await _conversation(make_harness, "one", "two", llm=llm)

    body = (await _recap(h, headers, cid)).json()

    assert body["recap"] == "가" * 300
    assert body["questions"] == ["다음 질문"]


async def test_another_users_recap_is_not_found(make_harness):
    h, headers, cid = await _conversation(make_harness, "one", "two")
    stranger = await h.guest()

    assert (await _recap(h, stranger, cid)).status_code == 404
