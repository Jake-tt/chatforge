async def test_guest_token_identifies_guest(h):
    headers = await h.guest()
    r = await h.client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["kind"] == "guest"
    assert r.json()["plan"] == "guest"


async def test_signup_then_login(h):
    r = await h.signup("Alice@Example.com")
    assert r.status_code == 201
    assert r.json()["user"]["email"] == "alice@example.com"
    assert r.json()["claim"] is None

    r = await h.client.post(
        "/auth/login", json={"email": "ALICE@example.com", "password": "password123"}
    )
    assert r.status_code == 200
    me = await h.client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.json() == {
        "kind": "user", "id": r.json()["user"]["id"], "plan": "free", "email": "alice@example.com"
    }


async def test_duplicate_email_rejected(h):
    assert (await h.signup("dup@example.com")).status_code == 201
    r = await h.signup("DUP@example.com")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "email_taken"


async def test_wrong_password_and_unknown_email_look_the_same(h):
    await h.signup("bob@example.com")
    wrong = await h.client.post(
        "/auth/login", json={"email": "bob@example.com", "password": "nope-nope"}
    )
    unknown = await h.client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_requests_without_or_with_bad_token_are_401(h):
    assert (await h.client.get("/conversations")).status_code == 401
    r = await h.client.get("/conversations", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_token"


async def test_guest_creation_is_rate_limited_per_ip(make_harness):
    h = await make_harness(guest_creations_per_hour_per_ip=2)
    assert (await h.client.post("/auth/guest")).status_code == 201
    assert (await h.client.post("/auth/guest")).status_code == 201
    r = await h.client.post("/auth/guest")
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rate_limited"
