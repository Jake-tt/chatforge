"""Claude on the wire: prompt caching, refusal fallback, structured output, usage parsing.

ChatAnthropic runs on the real Anthropic SDK with an in-memory httpx2 transport, so these
tests check the request that would actually be sent and parse a real-shaped response,
with no API key and no network.
"""

import json

import anthropic
import httpx2
from pydantic import BaseModel

from app.config import Settings
from app.services.chat_models import (
    LangChainProvider,
    build_chat_model,
    to_messages,
    usage_from_message,
)
from app.services.llm import ChatTurn, LLMError, Usage, build_provider

EPHEMERAL = {"type": "ephemeral"}


def _message(text: str = "hi", stop_reason: str = "end_turn", **usage) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 3, **usage}}


class Wire(httpx2.AsyncBaseTransport):
    """Records each request and answers with the next scripted response body."""

    def __init__(self, *responses: dict):
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=self._responses.pop(0))

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def _claude(purpose: str, wire: Wire):
    model = build_chat_model(Settings(_env_file=None, llm_provider="anthropic",
                                      anthropic_api_key="test"), purpose=purpose)
    # ChatAnthropic caches its SDK client; seed it with one that talks to `wire`.
    model.__dict__["_async_client"] = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0, http_client=httpx2.AsyncClient(transport=wire))
    return model


def _strip(blocks):
    """Message content without cache markers: the moving breakpoint isn't part of the
    prefix the cache matches."""
    if isinstance(blocks, str):
        return blocks
    return [{k: v for k, v in b.items() if k != "cache_control"} for b in blocks]


async def test_answers_mark_the_system_prompt_and_previous_turn_for_caching():
    wire = Wire(_message())
    turns = [ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
             ChatTurn("user", "q2", context="<sources>[1] fact</sources>")]

    await _claude("main", wire).ainvoke(to_messages("You are helpful.", turns, cache=True))

    body = wire.body()
    assert body["system"] == [{"type": "text", "text": "You are helpful.",
                               "cache_control": EPHEMERAL}]
    first, previous, current = body["messages"]
    assert first["content"] == [{"type": "text", "text": "q1"}]
    assert previous["content"] == [{"type": "text", "text": "a1", "cache_control": EPHEMERAL}]
    # Per-request sources ride on the current turn, after the breakpoint.
    assert current["content"] == [{"type": "text",
                                   "text": "<sources>[1] fact</sources>\n\nq2"}]


async def test_each_request_repeats_the_previous_prefix_byte_for_byte():
    wire = Wire(_message(), _message())
    model = _claude("main", wire)
    turn_1 = [ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
              ChatTurn("user", "q2", context="<sources>A</sources>")]
    turn_2 = [ChatTurn("user", "q1"), ChatTurn("assistant", "a1"), ChatTurn("user", "q2"),
              ChatTurn("assistant", "a2"), ChatTurn("user", "q3", context="<sources>B</sources>")]

    await model.ainvoke(to_messages("sys", turn_1, cache=True))
    await model.ainvoke(to_messages("sys", turn_2, cache=True))

    first, second = wire.body(0), wire.body(1)
    assert _strip(first["system"]) == _strip(second["system"])
    shared = len(first["messages"]) - 1  # everything before turn 1's current turn
    assert [_strip(m["content"]) for m in second["messages"][:shared]] == \
        [_strip(m["content"]) for m in first["messages"][:shared]]
    # Turn 1's sources were sent once and are not replayed as history.
    assert _strip(second["messages"][shared]["content"]) == [{"type": "text", "text": "q2"}]


async def test_answers_route_refusals_to_the_server_side_fallback():
    wire = Wire(_message())

    await _claude("main", wire).ainvoke(to_messages("sys", [ChatTurn("user", "hi")]))

    assert "server-side-fallback-2026-07-01" in wire.requests[0].headers["anthropic-beta"]
    assert wire.body()["fallbacks"] == "default"


async def test_cache_usage_from_the_api_reaches_billing():
    wire = Wire(_message(cache_read_input_tokens=800, cache_creation_input_tokens=40))

    reply = await _claude("main", wire).ainvoke(to_messages("sys", [ChatTurn("user", "hi")]))

    assert usage_from_message(reply) == Usage(12, 3, cache_read_tokens=800,
                                              cache_write_tokens=40)


class Decision(BaseModel):
    needs_search: bool
    query: str = ""


async def test_structured_calls_use_json_schema_output_not_a_tool():
    # Tool-based structured output can't be forced on this model: a reply in plain text
    # would fail to parse. A JSON-schema response format can't be ignored.
    wire = Wire(_message(json.dumps({"needs_search": True, "query": "한도"})))
    provider = LangChainProvider(_claude("aux", wire), structured_method="json_schema")

    decision, used = await provider.structured("route", "한도?", Decision)

    body = wire.body()
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in body and "tool_choice" not in body
    assert decision == Decision(needs_search=True, query="한도")
    assert used == Usage(12, 3)


async def test_a_refused_aux_call_returns_no_text():
    # A refused title or summary must not be stored as one.
    wire = Wire(_message("I can't help with that.", stop_reason="refusal"))
    provider = LangChainProvider(_claude("aux", wire))

    result = await provider.complete("title it", "something")

    assert (result.text, result.usage) == ("", Usage(12, 3))


async def test_a_refused_structured_call_is_not_retryable():
    wire = Wire(_message("", stop_reason="refusal"))
    provider = LangChainProvider(_claude("aux", wire), structured_method="json_schema")

    try:
        await provider.structured("route", "x", Decision)
    except LLMError as exc:
        assert not exc.retryable
    else:
        raise AssertionError("expected LLMError")


def test_real_providers_use_json_schema_for_structured_calls():
    for name in ("anthropic", "ollama"):
        provider = build_provider(Settings(_env_file=None, llm_provider=name,
                                           anthropic_api_key="test"))
        assert isinstance(provider, LangChainProvider)
        assert provider.structured_method == "json_schema"
