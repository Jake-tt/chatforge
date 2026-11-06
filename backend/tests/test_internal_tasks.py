"""Cloud Tasks callback endpoint (/internal/tasks/{name}) and the in-process queue."""

import asyncio
import threading

import pytest

from app.routers.internal import verify_task_caller
from app.services import tasks
from app.services.tasks import InlineTaskQueue


class RecordingQueue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []
        self.delays: list[int] = []  # parallel to `enqueued`

    async def enqueue(self, name: str, payload: dict, *, delay_seconds: int = 0) -> None:
        self.enqueued.append((name, payload))
        self.delays.append(delay_seconds)

    async def close(self) -> None:
        pass


@pytest.fixture
async def cloud_h(make_harness):
    queue = RecordingQueue()
    h = await make_harness(task_backend="cloudtasks", tasks=queue)
    return h, queue


async def test_callback_requires_oidc_token(cloud_h):
    h, _ = cloud_h
    r = await h.client.post("/internal/tasks/generate_title", json={})
    assert r.status_code == 401


async def test_chat_enqueues_title_task_and_callback_runs_it(cloud_h):
    h, queue = cloud_h
    h.app.dependency_overrides[verify_task_caller] = lambda: None
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    await h.chat(headers, cid, "plan a trip to busan")
    assert queue.enqueued == [
        ("generate_title", {"conversation_id": cid, "first_message": "plan a trip to busan"})
    ]

    name, payload = queue.enqueued[0]
    assert (await h.client.post(f"/internal/tasks/{name}", json=payload)).status_code == 204
    # Delivered twice (at-least-once): still fine, title isn't overwritten.
    assert (await h.client.post(f"/internal/tasks/{name}", json=payload)).status_code == 204

    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert detail["title"] == "plan a trip to busan"


async def test_unknown_task_is_404(cloud_h):
    h, _ = cloud_h
    h.app.dependency_overrides[verify_task_caller] = lambda: None
    assert (await h.client.post("/internal/tasks/nope", json={})).status_code == 404


async def test_internal_routes_not_mounted_with_inline_queue(h):
    assert (await h.client.post("/internal/tasks/generate_title", json={})).status_code == 404


async def test_a_delayed_inline_task_runs_later_without_holding_up_drain(monkeypatch):
    ran: list[str] = []

    async def handler(ctx, payload):
        ran.append(payload["n"])

    monkeypatch.setitem(tasks.HANDLERS, "probe", handler)
    queue = InlineTaskQueue(lambda: None)

    await queue.enqueue("probe", {"n": "later"}, delay_seconds=0.05)
    await queue.enqueue("probe", {"n": "now"})
    await queue.drain()  # waits for immediate work only: a delay can be an hour
    assert ran == ["now"]

    await asyncio.sleep(0.1)
    assert ran == ["now", "later"]


async def test_closing_the_inline_queue_drops_delayed_tasks(monkeypatch):
    ran: list[str] = []

    async def handler(ctx, payload):
        ran.append(payload["n"])

    monkeypatch.setitem(tasks.HANDLERS, "probe", handler)
    queue = InlineTaskQueue(lambda: None)

    await queue.enqueue("probe", {"n": "later"}, delay_seconds=0.05)
    await queue.close()  # shutdown must not wait out the delay
    await asyncio.sleep(0.1)

    assert ran == []


def test_drain_returns_when_a_task_finished_but_was_not_yet_removed():
    # A finished task leaves `_running` only in its done callback, on the next loop pass.
    # Drain called in between once spun forever: gather() over finished tasks returns
    # without yielding, so the callback never ran (CI hung on Postgres after a purge).
    async def drain_with_a_finished_task() -> None:
        queue = InlineTaskQueue(lambda: None)
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        queue._running.add(finished)  # the state between finishing and the callback
        await queue.drain()

    # A spinning drain never yields, so an asyncio timeout can't stop it: use a thread.
    worker = threading.Thread(target=asyncio.run, args=(drain_with_a_finished_task(),),
                              daemon=True)
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive(), "drain() spun on a task that had already finished"
