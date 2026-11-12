"""User memory graph: storage and the member's controls over it.

Facts are put in through `memory.add_fact`, the one write path the agent's tools and the
extraction task use; everything else is driven over HTTP.
"""

import uuid

from sqlalchemy import func, select

from app.models import MemoryEntity, MemoryFact, MemoryFactSource
from app.services import memory


async def _member(h, email: str = "mem@example.com") -> tuple[dict, uuid.UUID]:
    r = await h.signup(email)
    return {"Authorization": f"Bearer {r.json()['token']}"}, uuid.UUID(r.json()["user"]["id"])


async def _remember(h, user_id, cid, subject, predicate, obj=None, value=None):
    async with h.ctx.sessionmaker() as session:
        fact = await memory.add_fact(session, user_id, subject, predicate, obj=obj, value=value,
                                     conversation_id=uuid.UUID(cid))
        await session.commit()
        return str(fact.id)


async def _facts(h, headers) -> list[tuple[str, str, str]]:
    body = (await h.client.get("/memory", headers=headers)).json()
    return sorted((f["subject"], f["predicate"], f["object"]) for f in body["facts"])


async def _rows(h, model, user_id=None) -> int:
    async with h.ctx.sessionmaker() as session:
        stmt = select(func.count()).select_from(model)
        if user_id is not None and hasattr(model, "user_id"):
            stmt = stmt.where(model.user_id == user_id)
        return await session.scalar(stmt)


async def test_a_member_sees_and_deletes_what_is_remembered(h):
    headers, user_id = await _member(h)
    cid = await h.new_conversation(headers)
    coffee = await _remember(h, user_id, cid, "나", "좋아함", value="라떼")
    await _remember(h, user_id, cid, "나", "사는 곳", obj="부산")

    body = (await h.client.get("/memory", headers=headers)).json()
    assert body["enabled"] is True
    assert await _facts(h, headers) == [("나", "사는 곳", "부산"), ("나", "좋아함", "라떼")]

    assert (await h.client.delete(f"/memory/{coffee}", headers=headers)).status_code == 204
    assert await _facts(h, headers) == [("나", "사는 곳", "부산")]

    assert (await h.client.delete("/memory", headers=headers)).status_code == 204
    assert await _facts(h, headers) == []
    assert await _rows(h, MemoryEntity, user_id) == 0


async def test_the_same_fact_is_stored_once_with_each_source(h):
    headers, user_id = await _member(h)
    first, second = await h.new_conversation(headers), await h.new_conversation(headers)

    a = await _remember(h, user_id, first, "나", "좋아함", value="라떼")
    b = await _remember(h, user_id, second, " 나 ", "좋아함", value="라떼")

    assert a == b and await _rows(h, MemoryFactSource) == 2
    assert await _facts(h, headers) == [("나", "좋아함", "라떼")]


async def test_one_members_memory_is_invisible_to_another(h):
    headers, user_id = await _member(h)
    fact = await _remember(h, user_id, await h.new_conversation(headers), "나", "좋아함",
                           value="라떼")
    other, _ = await _member(h, "other@example.com")

    assert await _facts(h, other) == []
    assert (await h.client.delete(f"/memory/{fact}", headers=other)).status_code == 404
    assert await _facts(h, headers) == [("나", "좋아함", "라떼")]


async def test_guests_have_no_memory(h):
    guest = await h.guest()

    r = await h.client.get("/memory", headers=guest)

    assert (r.status_code, r.json()["detail"]["code"]) == (403, "members_only")


async def test_turning_memory_off_keeps_what_was_remembered(h):
    headers, user_id = await _member(h)
    await _remember(h, user_id, await h.new_conversation(headers), "나", "좋아함", value="라떼")

    r = await h.client.put("/memory/settings", headers=headers, json={"enabled": False})

    assert r.status_code == 200 and r.json() == {"enabled": False}
    body = (await h.client.get("/memory", headers=headers)).json()
    assert body["enabled"] is False and len(body["facts"]) == 1


async def test_deleting_a_conversation_forgets_what_came_only_from_it(h):
    headers, user_id = await _member(h)
    gone, kept = await h.new_conversation(headers), await h.new_conversation(headers)
    await _remember(h, user_id, gone, "나", "사는 곳", obj="부산")
    await _remember(h, user_id, gone, "나", "좋아함", value="라떼")
    await _remember(h, user_id, kept, "나", "좋아함", value="라떼")  # confirmed elsewhere too

    assert (await h.client.delete(f"/conversations/{gone}", headers=headers)).status_code == 204

    assert await _facts(h, headers) == [("나", "좋아함", "라떼")]
    assert await _rows(h, MemoryEntity, user_id) == 1  # "부산" isn't referenced any more


async def test_deleting_every_conversation_forgets_their_memories(h):
    headers, user_id = await _member(h)
    for _ in range(2):
        await _remember(h, user_id, await h.new_conversation(headers), "나", "좋아함",
                        value="라떼")

    assert (await h.client.delete("/conversations", headers=headers)).status_code == 200

    assert await _facts(h, headers) == []


async def test_an_account_purge_removes_its_memory(h):
    headers, user_id = await _member(h)
    await _remember(h, user_id, await h.new_conversation(headers), "나", "좋아함", value="라떼")
    other, other_id = await _member(h, "other@example.com")
    await _remember(h, other_id, await h.new_conversation(other), "나", "좋아함", value="녹차")

    r = await h.client.request("DELETE", "/auth/me", json={"password": "password123"},
                               headers=headers)
    assert r.status_code == 202
    await h.ctx.tasks.drain()

    assert await _rows(h, MemoryFact, user_id) == 0
    assert await _rows(h, MemoryEntity, user_id) == 0
    assert await _rows(h, MemoryFact, other_id) == 1  # nobody else's
