"""Cloud Tasks callback endpoint (/internal/tasks/{name})."""

import pytest

from app.routers.internal import verify_task_caller


class RecordingQueue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, name: str, payload: dict) -> None:
        self.enqueued.append((name, payload))

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
