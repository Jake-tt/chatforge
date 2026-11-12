"""Langfuse tracing behind a flag: off by default, never on Cloud Run, and never able to
break a reply.

The Langfuse handler is an external boundary: a recording LangChain callback stands in for
it, attached exactly where the real one would be.
"""

import sys

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import ValidationError

from app.config import Settings
from app.services.chat_models import LangChainProvider
from tests.fakes import ScriptedChatModel
from tests.test_agent import _answer, _route
from tests.test_internal_tasks import RecordingQueue


class RecordingHandler(BaseCallbackHandler):
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.chains: list[dict] = []  # metadata of each top-level chain run
        self.model_calls = 0

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       metadata=None, **kwargs):
        if parent_run_id is None:
            self.chains.append(dict(metadata or {}))

    def on_chat_model_start(self, serialized, messages, **kwargs):
        if self.fail:
            raise RuntimeError("tracing backend down")
        self.model_calls += 1


async def _traced(make_harness, handler, **settings):
    # The second scripted reply answers the follow-up suggestions call: not a valid
    # FollowUps, so no chips, but a traced model call all the same.
    router = ScriptedChatModel(script=[_route(), _route()])
    h = await make_harness(llm=LangChainProvider(router),
                           chat_model=ScriptedChatModel(script=[_answer("답변입니다")]),
                           tasks=RecordingQueue(),  # titles aren't under test
                           tracing_handler=handler, **settings)
    headers = {**await h.guest(), "X-Request-ID": "req-trace-1"}
    cid = await h.new_conversation(headers)
    return h, headers, cid


async def test_tracing_is_off_by_default(make_harness):
    handler = RecordingHandler()
    h, headers, cid = await _traced(make_harness, handler, suggestions_enabled=True)

    assert (await h.chat(headers, cid, "안녕")).last("done")
    assert handler.chains == [] and handler.model_calls == 0
    assert "langfuse" not in sys.modules


async def test_a_traced_turn_carries_its_request_and_conversation(make_harness):
    handler = RecordingHandler()
    h, headers, cid = await _traced(make_harness, handler, langfuse_enabled=True,
                                    suggestions_enabled=True)

    await h.chat(headers, cid, "안녕")

    turn = handler.chains[0]
    assert turn["request_id"] == "req-trace-1"
    assert turn["langfuse_session_id"] == cid
    # Router and answer inside the graph, and the follow-up suggestions after it.
    assert handler.model_calls >= 3
    assert any(m.get("request_id") == "req-trace-1" for m in handler.chains[1:])


async def test_a_failing_tracer_never_breaks_the_reply(make_harness):
    handler = RecordingHandler(fail=True)
    h, headers, cid = await _traced(make_harness, handler, langfuse_enabled=True)

    result = await h.chat(headers, cid, "안녕")

    assert result.last("done")["stop_reason"] == "end_turn"
    detail = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()
    assert [m["content"] for m in detail["messages"]] == ["안녕", "답변입니다"]
    assert (await h.client.get("/usage", headers=headers)).json()["used"] > 0


def test_tracing_cannot_be_turned_on_in_cloud_run(monkeypatch, make_settings):
    # Production traces would carry members' conversations: development and evaluation only.
    monkeypatch.setenv("K_SERVICE", "chatforge-api")
    with pytest.raises(ValidationError, match="LANGFUSE_ENABLED"):
        make_settings(langfuse_enabled=True)
    assert make_settings().langfuse_enabled is False


def test_settings_default_to_no_tracing():
    assert Settings.model_fields["langfuse_enabled"].default is False
