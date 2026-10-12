"""Callback endpoint for Cloud Tasks. Mounted only when TASK_BACKEND=cloudtasks."""

import logging

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.context import AppContext
from app.deps import get_ctx
from app.errors import ApiError
from app.services.tasks import HANDLERS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", include_in_schema=False)


async def verify_task_caller(request: Request, ctx: AppContext = Depends(get_ctx)) -> None:
    """Accept only OIDC tokens Google signed for our task-invoker service account."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise ApiError(401, "unauthenticated", "Missing OIDC token")

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    settings = ctx.settings
    try:
        claims = await run_in_threadpool(
            id_token.verify_oauth2_token,
            auth.removeprefix("Bearer "),
            google_requests.Request(),
            settings.tasks_service_url.rstrip("/"),
        )
    except ValueError as exc:
        raise ApiError(401, "invalid_token", "Invalid OIDC token") from exc
    if claims.get("email") != settings.tasks_invoker_sa or not claims.get("email_verified"):
        raise ApiError(403, "forbidden", "Unexpected task caller")


@router.post("/tasks/{name}", status_code=204, dependencies=[Depends(verify_task_caller)])
async def run_task(name: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> None:
    handler = HANDLERS.get(name)
    if handler is None:
        # 404 is permanent for Cloud Tasks only if the queue's retry config says so;
        # an unknown task name is a deploy mismatch, so log loudly.
        log.error("unknown task %s", name)
        raise ApiError(404, "unknown_task", f"Unknown task {name}")
    # Exceptions propagate as 500 so Cloud Tasks retries with backoff.
    await handler(ctx, await request.json())
