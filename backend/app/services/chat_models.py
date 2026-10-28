"""LangChain chat models for the agent graph.

One factory picks the provider from settings, so the graph never depends on a vendor:
- `ollama`: a local model (default Qwen3 8B) — free, runs on a 16GB Apple Silicon machine,
  and supports native tool calling, which the ReAct main agent needs.
- `anthropic`: Claude, keeping prompt caching and the weighted billing of cache tokens.
- `fake`: a deterministic echo model for tests and local runs without a model server.

Billing reads LangChain's standard `usage_metadata` and maps it onto `Usage`, whose
`input_tokens` means *uncached* input (what the Anthropic API reports), so the same
`Usage.billed()` weights apply whichever provider answered.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from app.config import Settings
from app.services.llm import (
    AUX_EFFORT,
    AUX_MAX_TOKENS,
    ChatTurn,
    Completion,
    LLMError,
    StreamEvent,
    TextDelta,
    TextResult,
    Usage,
)

Purpose = Literal["main", "aux"]  # main: the answering agent; aux: router, summaries

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_CACHE_WRITE_TTL_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def usage_from_message(message: BaseMessage) -> Usage:
    """Map LangChain `usage_metadata` (input_tokens = whole prompt) onto `Usage`."""
    meta = getattr(message, "usage_metadata", None)
    if not meta:
        return Usage()
    details = meta.get("input_token_details") or {}
    cache_read = details.get("cache_read") or 0
    # When the API splits cache writes by TTL, langchain-anthropic reports them under the
    # TTL keys and sets `cache_creation` to 0; reading only `cache_creation` would bill
    # cache writes as free.
    by_ttl = sum(details.get(k) or 0 for k in _CACHE_WRITE_TTL_KEYS)
    cache_write = by_ttl or details.get("cache_creation") or 0
    return Usage(
        input_tokens=max(meta.get("input_tokens", 0) - cache_read - cache_write, 0),
        output_tokens=meta.get("output_tokens", 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def build_chat_model(settings: Settings, *, purpose: Purpose) -> BaseChatModel:
    if settings.llm_provider == "ollama":
        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            num_ctx=settings.ollama_num_ctx,
            # Qwen3 thinks by default and, with reasoning unset, writes <think> tags into
            # the answer text. The agent needs plain answers and fast routing.
            reasoning=False,
            temperature=0,
        )
    if settings.llm_provider == "anthropic":
        main = purpose == "main"
        extra = {"api_key": settings.anthropic_api_key} if settings.anthropic_api_key else {}
        if main:
            # Answers: a refused request is retried server-side on the model the refusal
            # category calls for ("default"), so there is no fallback list to maintain.
            extra |= {"betas": [_FALLBACK_BETA], "model_kwargs": {"fallbacks": "default"}}
        return ChatAnthropic(
            model=settings.anthropic_model if main else settings.llm_aux_model,
            max_tokens=settings.llm_max_tokens if main else AUX_MAX_TOKENS,
            output_config={"effort": settings.llm_effort if main else AUX_EFFORT},
            **extra,
        )
    return EchoChatModel()


class EchoChatModel(BaseChatModel):
    """Echoes the last user message. Token counts are word counts so tests can assert."""

    @property
    def _llm_type(self) -> str:
        return "echo"

    def bind_tools(self, tools, **kwargs):
        # It never calls tools, so binding them changes nothing (lets the agent run on it).
        return self

    @staticmethod
    def _reply(messages: list[BaseMessage]) -> str:
        last = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        return f"(fake) You said: {last.content if last else ''}"

    @staticmethod
    def _usage(messages: list[BaseMessage], reply: str) -> dict:
        input_tokens = sum(len(str(m.content).split()) for m in messages)
        output_tokens = len(reply.split())
        return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens}

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None,
                  **kwargs: Any) -> ChatResult:
        reply = self._reply(messages)
        message = AIMessage(reply, usage_metadata=self._usage(messages, reply))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages: list[BaseMessage], stop: list[str] | None = None,
                run_manager: CallbackManagerForLLMRun | None = None,
                **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        yield from self._chunks(messages)

    async def _astream(self, messages: list[BaseMessage], stop: list[str] | None = None,
                       run_manager: AsyncCallbackManagerForLLMRun | None = None,
                       **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._chunks(messages):
            yield chunk

    def _chunks(self, messages: list[BaseMessage]) -> Iterator[ChatGenerationChunk]:
        reply = self._reply(messages)
        words = reply.split(" ")
        for i, word in enumerate(words):
            last = i == len(words) - 1
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=word if i == 0 else " " + word,
                # Usage rides on the final chunk, as streaming providers report it.
                usage_metadata=self._usage(messages, reply) if last else None,
            ))


_CACHE = {"type": "ephemeral"}


def to_messages(system: str, turns: list[ChatTurn], *, cache: bool = False
                ) -> list[BaseMessage]:
    """LangChain messages for a chat. With `cache`, mark the same prompt-cache breakpoints
    as the direct Anthropic path: the system prompt and the last turn the next request will
    repeat (the one before the current turn, whose `context` is never replayed)."""
    def body(text: str, breakpoint: bool) -> str | list[dict]:
        return [{"type": "text", "text": text, "cache_control": _CACHE}] if breakpoint else text

    messages: list[BaseMessage] = [SystemMessage(body(system, cache))]
    for i, turn in enumerate(turns):
        breakpoint = cache and i == len(turns) - 2
        if turn.role == "user":
            # Per-request context (sources) rides on the current turn only; see ChatTurn.
            text = f"{turn.context}\n\n{turn.content}" if turn.context else turn.content
            messages.append(HumanMessage(body(text, breakpoint)))
        else:
            messages.append(AIMessage(body(turn.content, breakpoint)))
    return messages


def stop_reason_of(message: BaseMessage | None) -> str:
    meta = message.response_metadata if message is not None else {}
    if meta.get("stop_reason"):
        return meta["stop_reason"]
    return "max_tokens" if meta.get("done_reason") == "length" else "end_turn"


class LangChainProvider:
    """`LLMProvider` on LangChain chat models, so every caller of the existing interface
    (RAG routing and compression, suggestions, titles, summaries) runs on any provider the
    factory supports, including a local model."""

    def __init__(self, main: BaseChatModel, aux: BaseChatModel):
        self._main = main
        self._aux = aux

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        full: AIMessageChunk | None = None
        try:
            async for chunk in self._main.astream(to_messages(system, turns)):
                if chunk.text:
                    yield TextDelta(chunk.text)
                full = chunk if full is None else full + chunk
        except Exception as exc:
            raise LLMError(str(exc), retryable=True) from exc
        used = usage_from_message(full) if full is not None else Usage()
        yield Completion(used.input_tokens, used.output_tokens, stop_reason_of(full),
                         used.cache_read_tokens, used.cache_write_tokens)

    async def complete(self, system: str, prompt: str) -> TextResult:
        try:
            message = await self._aux.ainvoke([SystemMessage(system), HumanMessage(prompt)])
        except Exception as exc:
            raise LLMError(str(exc), retryable=True) from exc
        return TextResult(message.text.strip(), usage_from_message(message))

    async def structured[M: BaseModel](
        self, system: str, prompt: str, schema: type[M]
    ) -> tuple[M, Usage]:
        runnable = self._aux.with_structured_output(schema, include_raw=True)
        try:
            out = await runnable.ainvoke([SystemMessage(system), HumanMessage(prompt)])
        except Exception as exc:
            raise LLMError(str(exc), retryable=True) from exc
        if out["parsed"] is None:
            # The tokens were spent even though nothing parsed; let the caller bill them.
            raise LLMError(f"no structured output ({out.get('parsing_error')})",
                           retryable=False, usage=usage_from_message(out["raw"]))
        return out["parsed"], usage_from_message(out["raw"])
