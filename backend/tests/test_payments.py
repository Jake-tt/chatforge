"""Payment webhooks: signed, idempotent, and applied in the order events happened."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import PaymentEvent

SECRET = "whsec-test-0123456789"
T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture
async def ph(make_harness):
    return await make_harness(payment_webhook_secret=SECRET)


def _signed(body: bytes, *, secret: str = SECRET, at: int | None = None) -> dict[str, str]:
    at = int(time.time()) if at is None else at
    digest = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Chatforge-Signature": f"t={at},v1={digest}", "Content-Type": "application/json"}


def _event(user_id: str, type_: str, *, id_: str | None = None, at: datetime = T0) -> bytes:
    return json.dumps({"id": id_ or f"evt_{uuid.uuid4().hex}", "type": type_,
                       "user_id": user_id, "occurred_at": at.isoformat()}).encode()


async def _deliver(h, body: bytes, headers: dict | None = None):
    return await h.client.post("/webhooks/payments", content=body,
                               headers=headers if headers is not None else _signed(body))


async def _member(h, email: str = "pay@example.com") -> tuple[dict, str]:
    r = await h.signup(email)
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()["user"]["id"]


async def _plan(h, headers) -> str:
    return (await h.client.get("/auth/me", headers=headers)).json()["plan"]


async def test_an_activated_subscription_upgrades_the_plan_and_its_limit(ph):
    headers, user_id = await _member(ph)

    r = await _deliver(ph, _event(user_id, "subscription.activated"))

    assert (r.status_code, r.json()["status"]) == (200, "applied")
    assert await _plan(ph, headers) == "pro"
    usage = (await ph.client.get("/usage", headers=headers)).json()
    assert usage["limit"] == ph.ctx.settings.pro_daily_tokens


async def test_a_redelivered_event_is_not_applied_twice(ph):
    # Providers retry until they see a 2xx, so the same event can arrive after a newer one.
    headers, user_id = await _member(ph)
    activated = _event(user_id, "subscription.activated", id_="evt_1", at=T0)
    await _deliver(ph, activated)
    await _deliver(ph, _event(user_id, "subscription.canceled", id_="evt_2",
                              at=T0 + timedelta(days=30)))

    again = await _deliver(ph, activated)

    assert (again.status_code, again.json()["status"]) == (200, "duplicate")
    assert await _plan(ph, headers) == "free"


async def test_an_event_that_arrives_late_cannot_undo_a_newer_one(ph):
    headers, user_id = await _member(ph)
    await _deliver(ph, _event(user_id, "subscription.canceled", at=T0 + timedelta(days=30)))

    late = await _deliver(ph, _event(user_id, "subscription.activated", at=T0))

    assert late.json()["status"] == "stale"
    assert await _plan(ph, headers) == "free"


async def test_concurrent_deliveries_of_one_event_apply_it_once(ph):
    headers, user_id = await _member(ph)
    body = _event(user_id, "subscription.activated", id_="evt_same")

    results = await asyncio.gather(*(_deliver(ph, body) for _ in range(5)))

    assert sorted(r.json()["status"] for r in results) == ["applied"] + ["duplicate"] * 4
    async with ph.ctx.sessionmaker() as session:
        assert len((await session.scalars(select(PaymentEvent))).all()) == 1


@pytest.mark.parametrize("tamper", ["body", "secret", "missing", "garbled", "old"])
async def test_unsigned_or_forged_deliveries_are_rejected(ph, tamper):
    headers, user_id = await _member(ph)
    body = _event(user_id, "subscription.activated")
    signed = {
        "body": _signed(body),
        "secret": _signed(body, secret="not-the-secret"),
        "missing": {"Content-Type": "application/json"},
        "garbled": {"Chatforge-Signature": "v1=zz", "Content-Type": "application/json"},
        # A captured delivery replayed later: the signature is valid but stale.
        "old": _signed(body, at=int(time.time()) - 3600),
    }[tamper]
    sent = body.replace(b"activated", b"canceled!") if tamper == "body" else body

    r = await _deliver(ph, sent, signed)

    assert (r.status_code, r.json()["detail"]["code"]) == (401, "invalid_signature")
    assert await _plan(ph, headers) == "free"


async def test_events_we_cannot_act_on_are_acknowledged_not_retried_forever(ph):
    # A non-2xx answer makes the provider retry; for these, retrying never helps.
    headers, user_id = await _member(ph)

    unknown_type = await _deliver(ph, _event(user_id, "invoice.created"))
    unknown_user = await _deliver(ph, _event(str(uuid.uuid4()), "subscription.activated"))

    assert unknown_type.json()["status"] == "ignored"
    assert unknown_user.json()["status"] == "unknown_user"
    assert await _plan(ph, headers) == "free"
    async with ph.ctx.sessionmaker() as session:
        assert {e.outcome for e in (await session.scalars(select(PaymentEvent))).all()} == \
            {"ignored", "unknown_user"}


async def test_a_deleted_account_is_not_upgraded(ph):
    headers, user_id = await _member(ph)
    await ph.client.request("DELETE", "/auth/me", json={"password": "password123"},
                            headers=headers)

    r = await _deliver(ph, _event(user_id, "subscription.activated"))

    assert r.json()["status"] == "unknown_user"


async def test_without_a_secret_there_is_no_webhook_endpoint(h):
    body = _event(str(uuid.uuid4()), "subscription.activated")
    assert (await _deliver(h, body)).status_code == 404
