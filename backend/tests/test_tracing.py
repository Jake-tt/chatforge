"""Langfuse tracing behind a flag: off by default, local only, never in production, and
never able to break a reply.

The Langfuse handler is an external boundary: a recording LangChain callback stands in for
it, attached exactly where the real one would be.
"""

import sys
import time

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import ValidationError

from app.config import Settings
from app.services.chat_models import LangChainProvider
from app.tracing import build_tracer, mask_images
from tests.fakes import ScriptedChatModel
from tests.test_agent import _answer, _route
from tests.test_internal_tasks import RecordingQueue

KEYS = {"langfuse_public_key": "pk-lf-local", "langfuse_secret_key": "sk-lf-local"}


class RecordingHandler(BaseCallbackHandler):
    def __init__(self, fail: bool = False, delay: float = 0.0):
        self.fail = fail
        self.delay = delay
        self.chains: list[dict] = []  # top-level chain runs: name and metadata
        self.models: list[dict] = []  # every model call: whether nested, and metadata

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       metadata=None, name=None, **kwargs):
        if parent_run_id is None:
            self.chains.append({"name": name, **(metadata or {})})

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None,
                            metadata=None, **kwargs):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("tracing backend down")
        self.models.append({"nested": parent_run_id is not None, **(metadata or {})})


async def _traced(make_harness, handler, *, tasks=None, routes=2, **settings):
    # Scripted replies past the first answer the calls after the graph (suggestions,
    # titles): not what those calls expect, so they come to nothing, but each is a traced
    # model call all the same.
    router = ScriptedChatModel(script=[_route()] * routes)
    h = await make_harness(llm=LangChainProvider(router),
                           chat_model=ScriptedChatModel(script=[_answer("답변입니다")]),
                           tasks=tasks or RecordingQueue(),
                           tracing_handler=handler, **settings)
    headers = {**await h.guest(), "X-Request-ID": "req-trace-1"}
    cid = await h.new_conversation(headers)
    return h, headers, cid


async def test_tracing_is_off_by_default(make_harness):
    handler = RecordingHandler()
    h, headers, cid = await _traced(make_harness, handler, suggestions_enabled=True)

    assert (await h.chat(headers, cid, "안녕")).last("done")
    assert handler.chains == [] and handler.models == []


def test_tracing_off_never_imports_langfuse(monkeypatch, make_settings):
    monkeypatch.setitem(sys.modules, "langfuse", None)  # any import of it would fail
    assert build_tracer(make_settings()).enabled is False


async def test_a_turn_is_one_trace_with_its_request_and_conversation(make_harness):
    handler = RecordingHandler()
    h, headers, cid = await _traced(make_harness, handler, langfuse_enabled=True,
                                    suggestions_enabled=True, **KEYS)

    await h.chat(headers, cid, "안녕")

    turn = [c for c in handler.chains if c["name"] == "chat_turn"]
    assert len(turn) == 1
    assert turn[0]["request_id"] == "req-trace-1" and turn[0]["langfuse_session_id"] == cid
    # Router and answer are inside the turn's trace, not traces of their own.
    inside = [m for m in handler.models if m.get("langfuse_session_id") == cid]
    assert len(inside) == 2 and all(m["nested"] for m in inside)
    # The follow-up suggestions come after the graph: their own trace, same request.
    after = [c for c in handler.chains if c["name"] != "chat_turn"]
    assert len(after) == 1 and after[0]["request_id"] == "req-trace-1"
    assert len(handler.models) == 3


async def test_background_calls_are_traced_with_their_request(make_harness):
    from app.services.tasks import InlineTaskQueue  # titles run in-process here

    handler = RecordingHandler()
    h, headers, cid = await _traced(make_harness, handler, langfuse_enabled=True,
                                    tasks=None, routes=2, **KEYS)
    h.ctx.tasks = InlineTaskQueue(lambda: h.ctx)

    await h.chat(headers, cid, "안녕")
    await h.ctx.tasks.drain()

    titles = [m for m in handler.models if not m["nested"]]
    assert len(titles) == 1 and titles[0]["request_id"] == "req-trace-1"


@pytest.mark.parametrize("handler", [RecordingHandler(fail=True), RecordingHandler(delay=0.2)],
                         ids=["failing", "slow"])
async def test_a_failing_or_slow_tracer_never_breaks_the_reply(make_harness, handler):
    h, headers, cid = await _traced(make_harness, handler, langfuse_enabled=True, **KEYS)

    result = await h.chat(headers, cid, "안녕")

    assert result.last("done")["stop_reason"] == "end_turn"
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert [m["content"] for m in detail["messages"]] == ["안녕", "답변입니다"]
    assert (await h.client.get("/usage", headers=headers)).json()["used"] > 0


def test_tracing_cannot_be_turned_on_in_cloud_run(monkeypatch, make_settings):
    # Production traces would carry members' conversations: development and evaluation only.
    monkeypatch.setenv("K_SERVICE", "chatforge-api")
    with pytest.raises(ValidationError, match="LANGFUSE_ENABLED"):
        make_settings(langfuse_enabled=True, **KEYS)
    assert make_settings().langfuse_enabled is False


def test_tracing_cannot_be_turned_on_in_prod():
    with pytest.raises(ValidationError, match="LANGFUSE_ENABLED"):
        Settings(env="prod", _env_file=None, langfuse_enabled=True, **KEYS)


def test_traces_only_go_to_a_local_langfuse(make_settings):
    # The SDK's default host is Langfuse Cloud: a missing or mistyped host must not send
    # conversations off the machine.
    for host in ("https://cloud.langfuse.com", "http://10.0.0.5:3000"):
        with pytest.raises(ValidationError, match="LANGFUSE_HOST"):
            make_settings(langfuse_enabled=True, langfuse_host=host, **KEYS)
    for host in ("http://localhost:3000", "http://127.0.0.1:3000"):
        assert make_settings(langfuse_enabled=True, langfuse_host=host, **KEYS).langfuse_host


def test_tracing_needs_its_keys(make_settings):
    with pytest.raises(ValidationError, match="LANGFUSE_PUBLIC_KEY"):
        make_settings(langfuse_enabled=True)


def test_images_are_masked_out_of_traces():
    data = {"messages": [[{"content": [
        {"type": "image", "base64": "iVBORw0KGgo" * 100, "mime_type": "image/png"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo"}},
        {"type": "text", "text": "이 사진 봐줘"}]}]]}

    masked = mask_images(data=data)

    assert "iVBORw0KGgo" not in str(masked)
    assert "이 사진 봐줘" in str(masked)


def test_settings_default_to_no_tracing():
    assert Settings.model_fields["langfuse_enabled"].default is False
