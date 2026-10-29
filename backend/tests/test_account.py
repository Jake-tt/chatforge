"""Revoking every token of an account, and deleting the account."""

import uuid

import jwt

from app.models import User
from app.security import create_token


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
