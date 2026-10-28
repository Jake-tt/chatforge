"""Conversation history cache: Redis list per conversation, validated by message seq."""

import asyncio
import json

from tests.fakes import RecordingChatModel
from tests.test_resilience import DownRedis


async def _setup(make_harness, **settings):
    llm = RecordingChatModel()
    h = await make_harness(chat_model=llm, **settings)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    owner = (await h.client.get("/auth/me", headers=headers)).json()
    key = f"history:guest:{owner['id']}:{cid}"
    return h, llm, headers, cid, key


async def test_cache_is_built_and_kept_in_sync(make_harness):
    h, _, headers, cid, key = await _setup(make_harness)
    await h.chat(headers, cid, "one")
    await h.chat(headers, cid, "two")

    cached = [json.loads(x) for x in await h.ctx.redis.lrange(key, 0, -1)]
    assert [(e["s"], e["r"], e["c"]) for e in cached] == [
        (1, "user", "one"), (2, "assistant", "reply 1"),
        (3, "user", "two"), (4, "assistant", "reply 2"),
    ]


async def test_context_is_read_from_cache_when_complete(make_harness):
    h, llm, headers, cid, key = await _setup(make_harness)
    await h.chat(headers, cid, "one")
    # Tamper with the cached copy only; if the next request uses it, Postgres wasn't read.
    first = json.loads(await h.ctx.redis.lindex(key, 0))
    await h.ctx.redis.lset(key, 0, json.dumps({**first, "c": "from-cache"}))

    await h.chat(headers, cid, "two")

    assert llm.contents() == ["from-cache", "reply 1", "two"]


async def test_gap_in_cache_falls_back_to_database(make_harness):
    h, llm, headers, cid, key = await _setup(make_harness)
    await h.chat(headers, cid, "one")
    await h.chat(headers, cid, "two")
    # Simulate the race where a stale re-fill lost a message: drop seq 2 from the list.
    await h.ctx.redis.lrem(key, 1, await h.ctx.redis.lindex(key, 1))

    await h.chat(headers, cid, "three")

    assert llm.contents() == [
        "one", "reply 1", "two", "reply 2", "three",
    ]
    seqs = [json.loads(x)["s"] for x in await h.ctx.redis.lrange(key, 0, -1)]
    assert seqs == [1, 2, 3, 4, 5, 6]  # rebuilt whole


async def test_expired_cache_falls_back_to_database(make_harness):
    h, llm, headers, cid, key = await _setup(make_harness)
    await h.chat(headers, cid, "one")
    await h.ctx.redis.delete(key)  # TTL expiry

    await h.chat(headers, cid, "two")

    # RPUSHX didn't create a tail-only list; the read rebuilt the full history.
    assert llm.contents() == ["one", "reply 1", "two"]


async def test_cache_window_is_trimmed_to_history_limit(make_harness):
    h, llm, headers, cid, key = await _setup(make_harness, history_limit=3)
    for text in ("one", "two", "three"):
        await h.chat(headers, cid, text)

    assert await h.ctx.redis.llen(key) == 3
    assert llm.contents() == ["two", "reply 2", "three"]


async def test_concurrent_messages_get_distinct_contiguous_seqs(make_harness):
    # The conversation lock normally rejects a second turn (409). With Redis down it fails
    # open, and the atomic seq allocation is what still keeps messages in order.
    h, _, headers, cid, _ = await _setup(make_harness, redis=DownRedis())
    results = await asyncio.gather(*(h.chat(headers, cid, f"m{i}") for i in range(3)))
    assert all(r.response.status_code == 200 for r in results)

    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert len(detail["messages"]) == 6
    roles = [m["role"] for m in detail["messages"]]
    assert roles.count("user") == roles.count("assistant") == 3


async def test_delete_all_conversations_clears_only_own_cache_keys(h):
    alice = await h.guest()
    bob = await h.guest()
    for _ in range(3):
        await h.chat(alice, await h.new_conversation(alice), "hi")
    bob_cid = await h.new_conversation(bob)
    await h.chat(bob, bob_cid, "hi")

    r = await h.client.delete("/conversations", headers=alice)

    assert r.json() == {"deleted": 3}
    assert (await h.client.get("/conversations", headers=alice)).json() == []
    keys = [k async for k in h.ctx.redis.scan_iter(match="history:*")]
    assert len(keys) == 1 and keys[0].endswith(bob_cid)


async def test_claim_drops_guest_cache_keys(h):
    r = await h.client.post("/auth/guest")
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    await h.chat(headers, await h.new_conversation(headers), "hi")
    assert [k async for k in h.ctx.redis.scan_iter(match="history:guest:*")]

    await h.signup("claim@example.com", guest_token=token)

    assert [k async for k in h.ctx.redis.scan_iter(match="history:guest:*")] == []
