"""Prompt caching on Claude: the request prefix stays byte-stable across turns and the
breakpoints sit at its end.

ChatAnthropic builds the request body from our LangChain messages; the tests inspect that
body, so no request leaves the process and no API key is needed.
"""

from app.config import Settings
from app.services.llm import ChatTurn

EPHEMERAL = {"type": "ephemeral"}


def _claude(**overrides):
    from app.services.chat_models import build_chat_model
    return build_chat_model(Settings(_env_file=None, llm_provider="anthropic",
                                     anthropic_api_key="test", **overrides), purpose="main")


def _payload(turns: list[ChatTurn]) -> dict:
    from app.services.chat_models import to_messages
    return _claude()._get_request_payload(to_messages("You are helpful.", turns, cache=True))


def test_claude_requests_mark_the_system_prompt_and_previous_turn_for_caching():
    body = _payload([ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
                     ChatTurn("user", "q2", context="<sources>[1] fact</sources>")])

    assert body["system"] == [{"type": "text", "text": "You are helpful.",
                               "cache_control": EPHEMERAL}]
    first, previous, current = body["messages"]
    assert first["content"] == "q1"
    assert previous["content"] == [{"type": "text", "text": "a1",
                                    "cache_control": EPHEMERAL}]
    # Per-request sources ride on the current turn, after the breakpoint.
    assert current["content"] == "<sources>[1] fact</sources>\n\nq2"


def test_claude_turn_sources_are_not_replayed_on_the_next_request():
    first = _payload([ChatTurn("user", "q1", context="<sources>A</sources>")])
    second = _payload([ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
                       ChatTurn("user", "q2", context="<sources>B</sources>")])

    assert first["system"] == second["system"]
    assert second["messages"][0]["content"] == "q1"


def test_claude_answers_route_refusals_to_the_server_side_fallback():
    body = _payload([ChatTurn("user", "hi")])

    assert body["betas"] == ["server-side-fallback-2026-07-01"]
    assert body["fallbacks"] == "default"


def test_anthropic_settings_build_a_langchain_provider():
    from app.services.chat_models import LangChainProvider
    from app.services.llm import build_provider

    provider = build_provider(Settings(_env_file=None, llm_provider="anthropic",
                                       anthropic_api_key="test"))

    assert isinstance(provider, LangChainProvider)
