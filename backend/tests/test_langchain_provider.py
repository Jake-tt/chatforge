"""LangChainProvider: the existing LLMProvider interface backed by LangChain chat models,
so routing, compression, suggestions, titles and summaries run on a local model too."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.config import Settings
from app.services.chat_models import LangChainProvider, to_messages
from app.services.llm import ChatTurn, LLMError, Usage, build_provider
from tests.fakes import ScriptedChatModel, structured, usage


def test_turn_context_is_sent_ahead_of_the_question_but_kept_out_of_history():
    turns = [ChatTurn("user", "q1"), ChatTurn("assistant", "a1"),
             ChatTurn("user", "q2", context="<sources>S</sources>")]

    sent = to_messages("sys", turns)

    assert isinstance(sent[0], SystemMessage) and sent[0].content == "sys"
    assert [m.content for m in sent[1:]] == ["q1", "a1", "<sources>S</sources>\n\nq2"]
    assert isinstance(sent[-1], HumanMessage)


async def test_complete_returns_text_and_usage_from_the_aux_model():
    aux = ScriptedChatModel(script=[AIMessage("a short title", usage_metadata=usage(30, 3))])
    provider = LangChainProvider(aux)

    result = await provider.complete("title it", "plan a trip")

    assert (result.text, result.usage) == ("a short title", Usage(30, 3))


class Decision(BaseModel):
    needs_search: bool
    query: str = ""


async def test_structured_output_is_parsed_and_charged():
    aux = ScriptedChatModel(script=[structured(needs_search=True, query="토큰 한도")])
    provider = LangChainProvider(aux)

    decision, used = await provider.structured("route", "한도?", Decision)

    assert decision == Decision(needs_search=True, query="토큰 한도")
    assert used == Usage(20, 4)


async def test_unparseable_structured_output_raises_llm_error():
    aux = ScriptedChatModel(script=[AIMessage("not a tool call", usage_metadata=usage(5, 5))])
    provider = LangChainProvider(aux)

    with pytest.raises(LLMError):
        await provider.structured("route", "한도?", Decision)


def test_ollama_settings_build_a_langchain_provider():
    provider = build_provider(Settings(_env_file=None, llm_provider="ollama"))

    assert isinstance(provider, LangChainProvider)
