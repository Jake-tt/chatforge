"""Prompt caching: the request prefix stays byte-stable across turns, breakpoints sit at its
end, and cache usage reported by the API reaches billing.

The Anthropic client runs against an in-memory httpx2 transport, so the real SDK builds and
parses the wire format while no request leaves the process.
"""

import json

import anthropic
import httpx2

from app.config import Settings
from app.services.llm import AnthropicProvider, ChatTurn, Completion, TextDelta

EPHEMERAL = {"type": "ephemeral"}


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _stream_body(text: str) -> bytes:
    return _sse([
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1,
                      "cache_read_input_tokens": 800, "cache_creation_input_tokens": 40},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ])


class CapturingTransport(httpx2.AsyncBaseTransport):
    def __init__(self):
        self.bodies: list[dict] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                              content=_stream_body("hello"))


def _provider() -> tuple[AnthropicProvider, CapturingTransport]:
    transport = CapturingTransport()
    client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                      http_client=httpx2.AsyncClient(transport=transport))
    return AnthropicProvider(Settings(_env_file=None), client=client), transport


async def _run(provider: AnthropicProvider, turns: list[ChatTurn]) -> list:
    return [event async for event in provider.stream("You are helpful.", turns)]


async def test_system_and_previous_turn_carry_cache_breakpoints():
    provider, transport = _provider()
    turns = [ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
             ChatTurn("user", "q2", context="<sources>[1] fact</sources>")]

    await _run(provider, turns)

    body = transport.bodies[0]
    assert body["system"] == [{"type": "text", "text": "You are helpful.",
                               "cache_control": EPHEMERAL}]
    first, previous, current = body["messages"]
    assert first["content"] == [{"type": "text", "text": "q1"}]
    # The last turn shared with the next request ends the cached prefix.
    assert previous["content"] == [{"type": "text", "text": "a1", "cache_control": EPHEMERAL}]
    # Per-request sources ride on the current turn, after the breakpoint, before the question.
    assert current["content"] == [{"type": "text", "text": "<sources>[1] fact</sources>"},
                                  {"type": "text", "text": "q2"}]


async def test_next_request_repeats_the_previous_prefix_byte_for_byte():
    provider, transport = _provider()
    turn1 = [ChatTurn("user", "q1", context="<sources>A</sources>")]
    turn2 = [ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
             ChatTurn("user", "q2", context="<sources>B</sources>")]

    await _run(provider, turn1)
    await _run(provider, turn2)

    def strip(blocks):
        return [{k: v for k, v in b.items() if k != "cache_control"} for b in blocks]

    first, second = transport.bodies
    assert strip(first["system"]) == strip(second["system"])
    # Turn 1's sources were sent once and are not replayed as history.
    assert strip(second["messages"][0]["content"]) == [{"type": "text", "text": "q1"}]


async def test_cache_usage_is_reported_on_completion():
    provider, _ = _provider()

    events = await _run(provider, [ChatTurn("user", "hi")])

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["hello"]
    completion = events[-1]
    assert isinstance(completion, Completion)
    assert (completion.input_tokens, completion.output_tokens) == (12, 3)
    assert (completion.cache_read_tokens, completion.cache_write_tokens) == (800, 40)
