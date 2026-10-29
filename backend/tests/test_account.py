"""Revoking every token of an account, and deleting the account."""

import uuid

import jwt
from sqlalchemy import func, select

from app.models import Conversation, Message, UsageDaily, User
from app.security import create_token
from app.services.claim import user_subject
from app.services.tasks import HANDLERS
from tests.test_internal_tasks import RecordingQueue


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(h, email: str, password: str = "password123"):
    return await h.client.post("/auth/login", json={"email": email, "password": password})


async def test_logging_out_everywhere_revokes_every_token(h):
    await h.signup("amy@example.com")
    laptop = (await _login(h, "amy@example.com")).json()["token"]
    phone = (await _login(h, "amy@example.com")).json()["token"]

    assert (await h.client.post("/auth/logout-all", headers=_bearer(laptop))).status_code == 204

    for token in (laptop, phone):
        r = await h.client.get("/auth/me", headers=_bearer(token))
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "token_revoked"
    fresh = (await _login(h, "amy@example.com")).json()["token"]
    assert (await h.client.get("/auth/me", headers=_bearer(fresh))).status_code == 200


async def test_tokens_issued_before_versioning_stay_valid(h):
    # Tokens from the previous release carry no version; logging everyone out on deploy
    # would be a surprise.
    r = await h.signup("ben@example.com")
    user_id = r.json()["user"]["id"]
    secret = h.ctx.settings.jwt_secret.get_secret_value()
    claims = jwt.decode(r.json()["token"], secret, algorithms=["HS256"])
    legacy = jwt.encode({k: v for k, v in claims.items() if k != "ver"}, secret,
                        algorithm="HS256")

    me = await h.client.get("/auth/me", headers=_bearer(legacy))
    assert (me.status_code, me.json()["id"]) == (200, user_id)


async def test_guests_have_nothing_to_log_out_everywhere(h):
    r = await h.client.post("/auth/logout-all", headers=await h.guest())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "members_only"


async def test_a_token_from_an_older_version_is_revoked(h):
    r = await h.signup("cara@example.com")
    user_id = r.json()["user"]["id"]
    async with h.ctx.sessionmaker() as session:
        user = await session.get(User, uuid.UUID(user_id))
        user.token_version = 3
        await session.commit()
    secret = h.ctx.settings.jwt_secret.get_secret_value()
    stale = create_token("user", user.id, secret, 60, version=2)
    current = create_token("user", user.id, secret, 60, version=3)

    assert (await h.client.get("/auth/me", headers=_bearer(stale))).status_code == 401
    assert (await h.client.get("/auth/me", headers=_bearer(current))).status_code == 200


async def _count(h, model, *where) -> int:
    async with h.ctx.sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


async def _member_with_chats(h, email: str, chats: int = 2):
    r = await h.signup(email)
    headers = _bearer(r.json()["token"])
    for i in range(chats):
        cid = await h.new_conversation(headers)
        await h.chat(headers, cid, f"메모 {i}")
    return headers, uuid.UUID(r.json()["user"]["id"])


async def _delete(h, headers, password: str = "password123"):
    return await h.client.request("DELETE", "/auth/me", json={"password": password},
                                  headers=headers)


async def test_deleting_an_account_answers_at_once_and_purges_in_the_background(
        make_harness):
    # Purging a large history inline once kept the request open for minutes. The request
    # only does what must be immediate; the rest is a background task.
    queue = RecordingQueue()
    h = await make_harness(tasks=queue)
    headers, user_id = await _member_with_chats(h, "dina@example.com")

    r = await _delete(h, headers)

    assert r.status_code == 202
    assert queue.enqueued[-1] == ("purge_user", {"user_id": str(user_id)})
    # Immediate: every token is dead, and the email can sign up again.
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 401
    assert (await _login(h, "dina@example.com")).status_code == 401
    assert (await h.signup("dina@example.com")).status_code == 201


async def test_the_purge_removes_the_account_and_everything_it_owned(make_harness):
    queue = RecordingQueue()
    h = await make_harness(tasks=queue, purge_batch_conversations=2)
    headers, user_id = await _member_with_chats(h, "eve@example.com", chats=5)
    other, other_id = await _member_with_chats(h, "fay@example.com", chats=1)
    await _delete(h, headers)

    payload = queue.enqueued[-1][1]
    await HANDLERS["purge_user"](h.ctx, payload)
    await HANDLERS["purge_user"](h.ctx, payload)  # delivered twice: still fine

    assert await _count(h, User, User.id == user_id) == 0
    assert await _count(h, Conversation, Conversation.user_id == user_id) == 0
    assert await _count(h, UsageDaily, UsageDaily.subject == user_subject(user_id)) == 0
    assert await _count(h, Message) == 2  # the other member's question and answer
    assert (await h.client.get("/conversations", headers=other)).status_code == 200


async def test_the_purge_drops_cached_histories(make_harness):
    queue = RecordingQueue()
    h = await make_harness(tasks=queue)
    headers, user_id = await _member_with_chats(h, "gil@example.com", chats=1)
    assert [k async for k in h.ctx.redis.scan_iter(f"history:{user_subject(user_id)}:*")]

    await _delete(h, headers)
    await HANDLERS["purge_user"](h.ctx, queue.enqueued[-1][1])

    assert not [k async for k in h.ctx.redis.scan_iter(f"history:{user_subject(user_id)}:*")]


async def test_deleting_needs_the_password(h):
    headers, user_id = await _member_with_chats(h, "hal@example.com", chats=1)

    r = await _delete(h, headers, password="not-my-password")

    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_credentials"
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 200


async def test_guests_cannot_delete_an_account(h):
    r = await _delete(h, await h.guest())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "members_only"


async def test_the_purge_never_touches_an_account_that_was_not_deleted(make_harness):
    # A forged or stale task must not wipe a live account.
    h = await make_harness(tasks=RecordingQueue())
    headers, user_id = await _member_with_chats(h, "ida@example.com", chats=1)

    await HANDLERS["purge_user"](h.ctx, {"user_id": str(user_id)})

    assert await _count(h, Conversation, Conversation.user_id == user_id) == 1
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 200


async def test_with_the_inline_queue_the_account_is_gone_once_tasks_finish(h):
    headers, user_id = await _member_with_chats(h, "jo@example.com", chats=1)

    assert (await _delete(h, headers)).status_code == 202
    await h.ctx.tasks.drain()

    assert await _count(h, User, User.id == user_id) == 0
    assert await _count(h, Message) == 0
