"""Background jobs.

Work that shouldn't block the chat response (e.g. titling a conversation) goes through a
`TaskQueue`. Locally it runs in-process; on Cloud Run it goes to Cloud Tasks, which calls
back into `/internal/tasks/{name}` with an OIDC token. Cloud Run may throttle CPU once a
response is sent, so in production background work must not rely on in-process tasks.

Handlers must be idempotent: Cloud Tasks delivers at least once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import update

from app.config import Settings
from app.models import Conversation

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

TaskHandler = Callable[["AppContext", dict], Awaitable[None]]

TITLE_SYSTEM = (
    "Write a short title (max 6 words) for a chat that starts with the user message below. "
    "Reply with the title only, no quotes or punctuation at the end."
)


async def generate_title(ctx: AppContext, payload: dict) -> None:
    conversation_id = uuid.UUID(payload["conversation_id"])
    title = (await ctx.llm.complete(TITLE_SYSTEM, payload["first_message"]))[:200].strip()
    if not title:
        return
    async with ctx.sessionmaker() as session:
        # Only fill an empty title: safe to re-run, and never clobbers a user rename.
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.title.is_(None))
            .values(title=title)
        )
        await session.commit()


HANDLERS: dict[str, TaskHandler] = {"generate_title": generate_title}


class TaskQueue(Protocol):
    async def enqueue(self, name: str, payload: dict) -> None: ...

    async def close(self) -> None: ...


class InlineTaskQueue:
    def __init__(self, ctx_getter: Callable[[], AppContext]):
        self._ctx_getter = ctx_getter
        self._running: set[asyncio.Task] = set()

    async def enqueue(self, name: str, payload: dict) -> None:
        task = asyncio.create_task(self._run(name, payload))
        self._running.add(task)  # keep a strong ref until done
        task.add_done_callback(self._running.discard)

    async def _run(self, name: str, payload: dict) -> None:
        try:
            await HANDLERS[name](self._ctx_getter(), payload)
        except Exception:
            log.exception("inline task %s failed", name)

    async def drain(self) -> None:
        """Wait for in-flight tasks (used by tests and graceful shutdown)."""
        while self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def close(self) -> None:
        await self.drain()


class CloudTasksQueue:
    def __init__(self, settings: Settings):
        from google.cloud import tasks_v2  # optional dependency: pip install .[gcp]

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksAsyncClient()
        self._parent = self._client.queue_path(
            settings.gcp_project, settings.gcp_location, settings.tasks_queue
        )
        self._base_url = settings.tasks_service_url.rstrip("/")
        self._invoker_sa = settings.tasks_invoker_sa

    async def enqueue(self, name: str, payload: dict) -> None:
        tasks_v2 = self._tasks_v2
        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{self._base_url}/internal/tasks/{name}",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode(),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._invoker_sa, audience=self._base_url
                ),
            )
        )
        await self._client.create_task(parent=self._parent, task=task)

    async def close(self) -> None:
        await self._client.transport.close()
