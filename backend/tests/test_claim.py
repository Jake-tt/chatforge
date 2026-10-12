"""Guest → member migration (signup/login with the guest token attached)."""


async def _guest_with_chat(h) -> tuple[str, dict, str, int]:
    r = await h.client.post("/auth/guest")
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    cid = await h.new_conversation(headers)
    result = await h.chat(headers, cid, "remember this")
    done = result.last("done")
    return token, headers, cid, done["input_tokens"] + done["output_tokens"]


async def test_signup_claims_guest_conversations_and_usage(h):
    guest_token, guest_headers, cid, guest_tokens = await _guest_with_chat(h)

    r = await h.signup("new@example.com", guest_token=guest_token)

    assert r.status_code == 201
    assert r.json()["claim"] == {
        "status": "claimed", "conversations_moved": 1, "tokens_merged": guest_tokens,
    }
    user_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    convs = (await h.client.get("/conversations", headers=user_headers)).json()
    assert [c["id"] for c in convs] == [cid]
    # Usage follows the data, so signing up doesn't reset the daily quota.
    assert (await h.client.get("/usage", headers=user_headers)).json()["used"] == guest_tokens

    # The old guest token is now rejected with a code the client can act on.
    r = await h.client.get("/conversations", headers=guest_headers)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "guest_claimed"


async def test_login_claim_is_idempotent(h):
    guest_token, _, _, guest_tokens = await _guest_with_chat(h)
    await h.signup("me@example.com", guest_token=guest_token)

    creds = {"email": "me@example.com", "password": "password123", "guest_token": guest_token}
    r = await h.client.post("/auth/login", json=creds)

    assert r.json()["claim"]["status"] == "already_claimed"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert len((await h.client.get("/conversations", headers=headers)).json()) == 1
    assert (await h.client.get("/usage", headers=headers)).json()["used"] == guest_tokens


async def test_guest_claimed_by_one_user_is_not_reclaimed_by_another(h):
    guest_token, _, _, _ = await _guest_with_chat(h)
    await h.signup("first@example.com", guest_token=guest_token)

    r = await h.signup("second@example.com", guest_token=guest_token)

    assert r.json()["claim"]["status"] == "claimed_by_other"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert (await h.client.get("/conversations", headers=headers)).json() == []


async def test_garbage_guest_token_never_blocks_login(h):
    r = await h.signup("ok@example.com", guest_token="not-a-jwt")
    assert r.status_code == 201
    assert r.json()["claim"] is None


async def test_user_token_in_guest_slot_is_ignored(h):
    user_token = (await h.signup("u1@example.com")).json()["token"]
    r = await h.signup("u2@example.com", guest_token=user_token)
    assert r.status_code == 201
    assert r.json()["claim"] is None
