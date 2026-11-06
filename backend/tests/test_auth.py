from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings


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


NOW = 1_800_000_450.0  # 450 s into both a 15-minute and an hour window


@pytest.fixture(autouse=True)
def _frozen_window(monkeypatch):
    # Fixed windows roll over on the clock; a rollover mid-test would reset a counter.
    monkeypatch.setattr("app.services.quota.time", SimpleNamespace(time=lambda: NOW))


def _from(xff: str) -> dict[str, str]:
    return {"X-Forwarded-For": xff}


async def test_client_ip_is_the_entry_the_trusted_proxy_appended(make_harness):
    # Cloud Run's front end appends the address it saw; anything to its left came from the
    # client and can be forged, so it must not open a new rate-limit bucket.
    h = await make_harness(guest_creations_per_hour_per_ip=1, trusted_proxy_hops=1)
    first = await h.client.post("/auth/guest", headers=_from("1.1.1.1, 9.9.9.9"))
    spoofed = await h.client.post("/auth/guest", headers=_from("2.2.2.2, 9.9.9.9"))
    other_client = await h.client.post("/auth/guest", headers=_from("8.8.8.8"))
    assert (first.status_code, spoofed.status_code, other_client.status_code) == (201, 429, 201)


async def test_each_single_entry_from_the_trusted_proxy_is_its_own_client(make_harness):
    # The usual Cloud Run request: the front end's entry is the only one.
    h = await make_harness(guest_creations_per_hour_per_ip=1, trusted_proxy_hops=1)
    assert (await h.client.post("/auth/guest", headers=_from("1.1.1.1"))).status_code == 201
    assert (await h.client.post("/auth/guest", headers=_from("2.2.2.2"))).status_code == 201


async def test_forwarded_entries_are_read_across_header_lines_and_spacing(make_harness):
    h = await make_harness(guest_creations_per_hour_per_ip=1, trusted_proxy_hops=1)
    first = await h.client.post("/auth/guest", headers=_from("1.1.1.1, 9.9.9.9"))
    assert first.status_code == 201
    split = [("x-forwarded-for", "2.2.2.2"), ("x-forwarded-for", " 9.9.9.9 ,")]
    assert (await h.client.post("/auth/guest", headers=split)).status_code == 429


async def test_ipv6_clients_share_a_bucket_per_64_prefix(make_harness):
    # One subscriber usually gets a whole /64; rotating within it must not reset the limit.
    h = await make_harness(guest_creations_per_hour_per_ip=1, trusted_proxy_hops=1)
    assert (await h.client.post("/auth/guest", headers=_from("2001:db8:1:2::1"))).status_code == 201
    same_64 = await h.client.post("/auth/guest", headers=_from("2001:db8:1:2:ffff::9"))
    other_64 = await h.client.post("/auth/guest", headers=_from("2001:db8:1:3::1"))
    assert (same_64.status_code, other_64.status_code) == (429, 201)


async def test_forwarded_headers_are_ignored_without_a_trusted_proxy(make_harness):
    h = await make_harness(guest_creations_per_hour_per_ip=1)
    assert (await h.client.post("/auth/guest", headers=_from("1.1.1.1"))).status_code == 201
    assert (await h.client.post("/auth/guest", headers=_from("2.2.2.2"))).status_code == 429


async def test_a_request_that_skipped_a_proxy_falls_back_to_the_peer(make_harness):
    # Fewer entries than proxies means the chain is not what we expect; don't trust any.
    h = await make_harness(guest_creations_per_hour_per_ip=1, trusted_proxy_hops=2)
    assert (await h.client.post("/auth/guest", headers=_from("1.1.1.1"))).status_code == 201
    assert (await h.client.post("/auth/guest", headers=_from("2.2.2.2"))).status_code == 429


async def test_signups_are_rate_limited_per_ip(make_harness):
    h = await make_harness(signups_per_hour_per_ip=2)
    assert (await h.signup("a1@example.com")).status_code == 201
    assert (await h.signup("a2@example.com")).status_code == 201
    r = await h.signup("a3@example.com")
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rate_limited"
    assert r.headers["Retry-After"] == str(3600 - 450)  # until the window rolls over


async def _login(h, email: str, password: str):
    return await h.client.post("/auth/login", json={"email": email, "password": password})


async def test_repeated_wrong_passwords_lock_that_email_only(make_harness):
    h = await make_harness(login_failures_per_15min_per_email=2)
    await h.signup("carol@example.com")
    await h.signup("dave@example.com")
    assert (await _login(h, "carol@example.com", "wrong-one")).status_code == 401
    assert (await _login(h, "CAROL@example.com", "wrong-two")).status_code == 401

    locked = await _login(h, "carol@example.com", "password123")
    assert locked.status_code == 429
    assert locked.json()["detail"]["code"] == "rate_limited"
    assert locked.headers["Retry-After"] == str(900 - 450)
    assert (await _login(h, "dave@example.com", "password123")).status_code == 200


async def test_a_successful_login_clears_earlier_failures(make_harness):
    h = await make_harness(login_failures_per_15min_per_email=2)
    await h.signup("erin@example.com")
    assert (await _login(h, "erin@example.com", "wrong-one")).status_code == 401
    assert (await _login(h, "erin@example.com", "password123")).status_code == 200
    assert (await _login(h, "erin@example.com", "wrong-two")).status_code == 401
    assert (await _login(h, "erin@example.com", "password123")).status_code == 200


async def test_an_unknown_email_locks_the_same_way(make_harness):
    # Otherwise the lock would reveal which emails have accounts.
    h = await make_harness(login_failures_per_15min_per_email=1)
    await h.signup("gina@example.com")
    for email in ("gina@example.com", "nobody@example.com"):
        assert (await _login(h, email, "wrong-one")).status_code == 401
    known = await _login(h, "gina@example.com", "wrong-two")
    unknown = await _login(h, "nobody@example.com", "wrong-two")
    assert known.status_code == unknown.status_code == 429
    assert known.json() == unknown.json()


async def test_login_attempts_are_rate_limited_per_ip(make_harness):
    # Spraying one password across many emails never trips the per-email limit.
    h = await make_harness(logins_per_15min_per_ip=2)
    assert (await _login(h, "x1@example.com", "password123")).status_code == 401
    assert (await _login(h, "x2@example.com", "password123")).status_code == 401
    assert (await _login(h, "x3@example.com", "password123")).status_code == 429


async def test_rate_limit_keys_do_not_store_raw_emails(make_harness):
    h = await make_harness(login_failures_per_15min_per_email=5)
    await _login(h, "frank@example.com", "wrong-one")
    keys = [k async for k in h.ctx.redis.scan_iter("rl:*")]
    assert keys and not any("frank" in k for k in keys)


def test_prod_needs_the_proxy_hop_count_set_explicitly(monkeypatch):
    # Left at 0 behind Cloud Run, every client would share the front end's address.
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    prod = {"env": "prod", "jwt_secret": "x" * 40, "_env_file": None}
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_HOPS"):
        Settings(**prod)
    assert Settings(**prod, trusted_proxy_hops=1).trusted_proxy_hops == 1
