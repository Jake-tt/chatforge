"""Payment webhooks: verify, record once, apply in the order events happened.

- Signed: the provider signs `<timestamp>.<raw body>` with a shared secret (HMAC-SHA256).
  A signature older than the tolerance is refused, so a captured delivery can't be
  replayed later.
- Idempotent: the provider's event id is the primary key of `payment_events`. A redelivery
  conflicts on insert and changes nothing, however late it comes.
- Ordered: a user's plan follows their newest event by `occurred_at`. An event that
  arrives after a newer one is recorded as stale, not applied. The user row is locked
  first, so two events for one user are decided one after the other.
- Acknowledged: an event we can't act on (unknown type, unknown or deleted user) is still
  recorded and answered with 2xx; a retry would never succeed and would only repeat.
"""

import hashlib
import hmac
import time
import uuid
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import exists, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentEvent, User

SIGNATURE_TOLERANCE_SECONDS = 300
PLAN_FOR_EVENT = {"subscription.activated": "pro", "subscription.canceled": "free"}


class InvalidSignature(Exception):
    pass


class PaymentEventIn(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    type: str = Field(max_length=64)
    user_id: uuid.UUID
    occurred_at: datetime


def verify_signature(header: str | None, body: bytes, secret: str,
                     now: float | None = None) -> None:
    """Raise unless `header` ("t=<unix>,v1=<hex>") signs `body` within the tolerance."""
    try:
        fields = dict(part.split("=", 1) for part in (header or "").split(","))
        at, given = int(fields["t"]), fields["v1"]
    except (KeyError, ValueError) as exc:
        raise InvalidSignature("malformed signature header") from exc
    if abs((time.time() if now is None else now) - at) > SIGNATURE_TOLERANCE_SECONDS:
        raise InvalidSignature("signature timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise InvalidSignature("signature mismatch")


async def record_event(session: AsyncSession, event: PaymentEventIn) -> str:
    """Record and apply one event in the caller's transaction; returns its outcome, or
    "duplicate" if it was recorded before."""
    insert = postgresql.insert if session.get_bind().dialect.name == "postgresql" \
        else sqlite.insert
    # Placeholder outcome; set below once decided, in the same transaction.
    inserted = await session.scalar(
        insert(PaymentEvent)
        .values(id=event.id, type=event.type, user_id=event.user_id,
                occurred_at=event.occurred_at, outcome="pending")
        .on_conflict_do_nothing(index_elements=[PaymentEvent.id])
        .returning(PaymentEvent.id))
    if inserted is None:
        return "duplicate"
    outcome = await _apply(session, event)
    await session.execute(update(PaymentEvent).where(PaymentEvent.id == event.id)
                          .values(outcome=outcome))
    return outcome


async def _apply(session: AsyncSession, event: PaymentEventIn) -> str:
    plan = PLAN_FOR_EVENT.get(event.type)
    if plan is None:
        return "ignored"
    user = await session.scalar(
        select(User).where(User.id == event.user_id, User.deleted_at.is_(None))
        .with_for_update())
    if user is None:
        return "unknown_user"
    newer = await session.scalar(select(exists().where(
        PaymentEvent.user_id == event.user_id,
        PaymentEvent.type.in_(PLAN_FOR_EVENT),
        PaymentEvent.occurred_at > event.occurred_at,
        PaymentEvent.id != event.id)))
    if newer:
        return "stale"
    user.plan = plan
    return "applied"
