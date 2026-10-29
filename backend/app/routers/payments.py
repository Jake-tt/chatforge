"""Payment provider webhooks. Mounted only when PAYMENT_WEBHOOK_SECRET is set."""

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.deps import get_ctx, get_session
from app.errors import ApiError
from app.services.payments import InvalidSignature, PaymentEventIn, record_event, verify_signature

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", include_in_schema=False)


@router.post("/payments")
async def payment_webhook(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # The signature covers the raw bytes, so verify before parsing anything.
    body = await request.body()
    try:
        verify_signature(request.headers.get("chatforge-signature"), body,
                         ctx.settings.payment_webhook_secret.get_secret_value())
    except InvalidSignature as exc:
        log.warning("payment webhook rejected: %s", exc)
        raise ApiError(401, "invalid_signature", "Invalid webhook signature") from exc
    try:
        event = PaymentEventIn.model_validate_json(body)
    except ValidationError as exc:
        raise ApiError(400, "invalid_event", "Malformed webhook event") from exc

    status = await record_event(session, event)
    await session.commit()
    log.info("payment event", extra={"event_id": event.id, "event_type": event.type,
                                     "status": status})
    return {"status": status}
