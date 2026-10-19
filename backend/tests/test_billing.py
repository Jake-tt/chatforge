"""Quota charges in input-token equivalents: output and cache tokens are weighted by price."""

from collections.abc import AsyncIterator

from app.services.llm import ChatTurn, Completion, StreamEvent, TextDelta, TextResult, Usage


class CachedProvider:
    """Reports a reply whose prompt was mostly served from the prompt cache."""

    def __init__(self, completion: Completion):
        self._completion = completion

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        yield TextDelta("cached answer")
        yield self._completion

    async def complete(self, system: str, prompt: str) -> TextResult:
        return TextResult("", Usage())


async def test_output_and_cache_tokens_are_weighted(make_harness):
    llm = CachedProvider(Completion(input_tokens=100, output_tokens=10, stop_reason="end_turn",
                                    cache_read_tokens=1000, cache_write_tokens=200))
    h = await make_harness(llm=llm)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    done = (await h.chat(headers, cid, "hi")).last("done")

    # 100 input + 10 output x5 + 1000 cache reads x0.05 + 200 cache writes x1.25
    assert done["billed_tokens"] == 100 + 50 + 50 + 250
    assert (done["cache_read_tokens"], done["cache_write_tokens"]) == (1000, 200)
    assert (await h.client.get("/usage", headers=headers)).json()["used"] == 450
    # The stored message keeps the full prompt size, cached or not.
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert detail["messages"][-1]["input_tokens"] == 100 + 1000 + 200


async def test_fractional_charge_rounds_up(make_harness):
    # A fully cached prompt must still cost something, or cache hits would be free.
    llm = CachedProvider(Completion(input_tokens=0, output_tokens=0, stop_reason="end_turn",
                                    cache_read_tokens=3))
    h = await make_harness(llm=llm)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    done = (await h.chat(headers, cid, "hi")).last("done")

    assert done["billed_tokens"] == 1
    assert (await h.client.get("/usage", headers=headers)).json()["used"] == 1


async def test_weights_follow_settings(make_harness):
    llm = CachedProvider(Completion(input_tokens=10, output_tokens=10, stop_reason="end_turn",
                                    cache_read_tokens=10, cache_write_tokens=10))
    h = await make_harness(llm=llm, token_weight_output=1.0, token_weight_cache_read=0.1,
                           token_weight_cache_write=2.0)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    done = (await h.chat(headers, cid, "hi")).last("done")

    assert done["billed_tokens"] == 10 + 10 + 1 + 20
