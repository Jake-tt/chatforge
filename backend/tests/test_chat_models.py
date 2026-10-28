"""LangChain chat model layer: provider factory and usage_metadata → Usage for billing."""

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from app.config import Settings
from app.services.chat_models import EchoChatModel, build_chat_model, usage_from_message
from app.services.llm import Usage


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_anthropic_cache_tokens_split_by_ttl_are_still_billed_as_writes():
    # langchain-anthropic reports input_tokens as the full prompt and, when the API splits
    # cache writes by TTL, zeroes `cache_creation` and puts the count under the TTL keys.
    message = AIMessage("hi", usage_metadata={
        "input_tokens": 1300, "output_tokens": 10, "total_tokens": 1310,
        "input_token_details": {"cache_read": 1000, "cache_creation": 0,
                                "ephemeral_5m_input_tokens": 200},
    })

    assert usage_from_message(message) == Usage(input_tokens=100, output_tokens=10,
                                                 cache_read_tokens=1000, cache_write_tokens=200)


def test_cache_creation_without_ttl_split_is_used_as_is():
    message = AIMessage("hi", usage_metadata={
        "input_tokens": 300, "output_tokens": 5, "total_tokens": 305,
        "input_token_details": {"cache_read": 0, "cache_creation": 250},
    })

    assert usage_from_message(message) == Usage(50, 5, 0, 250)


def test_models_without_cache_details_bill_plain_tokens():
    message = AIMessage("hi", usage_metadata={"input_tokens": 50, "output_tokens": 7,
                                              "total_tokens": 57})

    assert usage_from_message(message) == Usage(50, 7)
    assert usage_from_message(AIMessage("no usage reported")) == Usage()


def test_ollama_model_runs_qwen3_with_thinking_off():
    # With reasoning left at None, Qwen3 puts <think>…</think> into the answer text.
    model = build_chat_model(_settings(llm_provider="ollama"), purpose="main")

    assert isinstance(model, ChatOllama)
    assert (model.model, model.reasoning) == ("qwen3:8b", False)
    assert model.base_url == "http://localhost:11434"


def test_anthropic_model_uses_main_and_aux_settings():
    settings = _settings(llm_provider="anthropic", anthropic_model="claude-opus-5-5",
                         llm_aux_model="claude-sonnet-5-5", llm_effort="medium",
                         anthropic_api_key="test")

    main = build_chat_model(settings, purpose="main")
    aux = build_chat_model(settings, purpose="aux")

    assert isinstance(main, ChatAnthropic) and isinstance(aux, ChatAnthropic)
    assert (main.model, main.output_config) == ("claude-opus-5-5", {"effort": "medium"})
    assert (aux.model, aux.output_config) == ("claude-sonnet-5-5", {"effort": "low"})


async def test_fake_model_streams_an_echo_with_word_count_usage():
    model = build_chat_model(_settings(llm_provider="fake"), purpose="main")
    assert isinstance(model, EchoChatModel)
    messages = [SystemMessage("be brief"), HumanMessage("hello there")]

    chunks = [chunk async for chunk in model.astream(messages)]
    full = chunks[0]
    for chunk in chunks[1:]:
        full += chunk

    assert full.content == "(fake) You said: hello there"
    # One chunk per word; LangChain appends an empty closing chunk of its own.
    assert [c.content for c in chunks if c.content] == [
        "(fake)", " You", " said:", " hello", " there"]
    assert usage_from_message(full) == Usage(input_tokens=4, output_tokens=5)
