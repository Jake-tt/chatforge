"""Quota charges in input-token equivalents: output and cache tokens are weighted by price."""

from tests.fakes import NoRouterProvider, ScriptedChatModel, cached_answer


async def _billing_harness(make_harness, answer, **settings):
    return await make_harness(llm=NoRouterProvider(),
                              chat_model=ScriptedChatModel(script=[answer]), **settings)


async def test_output_and_cache_tokens_are_weighted(make_harness):
    h = await _billing_harness(make_harness, cached_answer(100, 10, cache_read=1000,
                                                           cache_write=200))
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
    h = await _billing_harness(make_harness, cached_answer(0, 0, cache_read=3))
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    done = (await h.chat(headers, cid, "hi")).last("done")

    assert done["billed_tokens"] == 1
    assert (await h.client.get("/usage", headers=headers)).json()["used"] == 1


async def test_weights_follow_settings(make_harness):
    h = await _billing_harness(make_harness, cached_answer(10, 10, cache_read=10,
                                                           cache_write=10),
                               token_weight_output=1.0, token_weight_cache_read=0.1,
                               token_weight_cache_write=2.0)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    done = (await h.chat(headers, cid, "hi")).last("done")

    assert done["billed_tokens"] == 10 + 10 + 1 + 20
