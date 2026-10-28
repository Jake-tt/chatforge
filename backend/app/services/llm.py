"""LLM provider abstraction.

Routes only depend on `LLMProvider`, so the real Anthropic client and the deterministic
`FakeProvider` (local dev without an API key, and tests) are interchangeable.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import anthropic
from pydantic import BaseModel

if TYPE_CHECKING:
    from app.config import Settings  # config imports TokenWeights from here


# Router, summaries, titles: short outputs at low effort, whichever client makes the call.
AUX_MAX_TOKENS = 2048
AUX_EFFORT = "low"


@dataclass(frozen=True)
class ChatTurn:
    role: str  # "user" | "assistant"
    content: str
    # Per-request material for this turn only (e.g. retrieved sources). Sent ahead of
    # `content` but never stored, so replayed history stays byte-identical across requests.
    context: str = ""


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completion:
    input_tokens: int  # uncached input only, as the API reports it
    output_tokens: int
    stop_reason: str | None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


StreamEvent = TextDelta | Completion


@dataclass(frozen=True)
class TokenWeights:
    """Price of each token kind relative to one uncached input token."""

    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens,
                     self.cache_read_tokens + other.cache_read_tokens,
                     self.cache_write_tokens + other.cache_write_tokens)

    @property
    def prompt_tokens(self) -> int:
        """Whole prompt size: the API's `input_tokens` excludes cached tokens."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def billed(self, w: TokenWeights) -> int:
        """Charge in input-token equivalents, so quotas track cost rather than raw counts.

        Rounded up: a fully cached reply still costs at least one token.
        """
        return math.ceil(self.input_tokens + self.output_tokens * w.output
                         + self.cache_read_tokens * w.cache_read
                         + self.cache_write_tokens * w.cache_write)

    @classmethod
    def of(cls, completion: Completion) -> Usage:
        return cls(completion.input_tokens, completion.output_tokens,
                   completion.cache_read_tokens, completion.cache_write_tokens)


def _usage_from_api(usage) -> Usage:
    # Cache fields are absent (None) when the request had no cache breakpoint.
    return Usage(usage.input_tokens, usage.output_tokens,
                 usage.cache_read_input_tokens or 0, usage.cache_creation_input_tokens or 0)


@dataclass(frozen=True)
class TextResult:
    text: str
    usage: Usage


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool, usage: Usage | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage or Usage()  # tokens spent before the failure, still billable


class LLMProvider(Protocol):
    def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        """Yield TextDelta chunks, then exactly one Completion."""
        ...

    async def complete(self, system: str, prompt: str) -> TextResult: ...

    async def structured[M: BaseModel](
        self, system: str, prompt: str, schema: type[M]
    ) -> tuple[M, Usage]:
        """Return an instance of `schema`; raise LLMError if the model can't produce one."""
        ...


_CACHE = {"type": "ephemeral"}


def _cached_messages(turns: list[ChatTurn]) -> list[dict]:
    """Render turns with a cache breakpoint on the last one the next request will repeat.

    Caching is a prefix match, and the current turn is the one part that changes: its
    `context` is never replayed. So the breakpoint goes on the turn before it. The next
    request writes one turn further on and its lookback finds this entry, so each request
    reads the whole conversation up to the previous reply from cache.
    """
    messages = []
    for i, turn in enumerate(turns):
        blocks = [{"type": "text", "text": turn.context}] if turn.context else []
        blocks.append({"type": "text", "text": turn.content})
        if i == len(turns) - 2:
            blocks[-1]["cache_control"] = _CACHE
        messages.append({"role": turn.role, "content": blocks})
    return messages


class AnthropicProvider:
    # "default" routes by refusal category, so no fallback model list to maintain.
    _FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None):
        # Same key source as the LangChain models: Settings reads ANTHROPIC_API_KEY from
        # the environment or .env; the SDK alone would only see the environment.
        key = settings.anthropic_api_key
        self._client = client or anthropic.AsyncAnthropic(
            **({"api_key": key.get_secret_value()} if key else {}))
        self._model = settings.anthropic_model
        self._aux_model = settings.llm_aux_model  # router, summaries, titles
        self._effort = settings.llm_effort
        self._max_tokens = settings.llm_max_tokens

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        try:
            async with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": _CACHE}],
                messages=_cached_messages(turns),
                output_config={"effort": self._effort},
                betas=[self._FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                async for text in stream.text_stream:
                    yield TextDelta(text)
                final = await stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise LLMError(str(exc), retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(str(exc), retryable=exc.status_code >= 500) from exc
        usage = _usage_from_api(final.usage)
        yield Completion(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            stop_reason=final.stop_reason,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    async def complete(self, system: str, prompt: str) -> TextResult:
        try:
            response = await self._client.messages.create(
                model=self._aux_model,
                max_tokens=AUX_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": AUX_EFFORT},
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc), retryable=True) from exc
        usage = _usage_from_api(response.usage)
        if response.stop_reason == "refusal":
            return TextResult("", usage)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return TextResult(text, usage)

    async def structured[M: BaseModel](
        self, system: str, prompt: str, schema: type[M]
    ) -> tuple[M, Usage]:
        try:
            response = await self._client.messages.parse(
                model=self._aux_model,
                max_tokens=AUX_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": AUX_EFFORT},
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc), retryable=True) from exc
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise LLMError(f"no structured output (stop_reason={response.stop_reason})",
                           retryable=False)
        return response.parsed_output, _usage_from_api(response.usage)


class FakeProvider:
    """Deterministic echo model. Token counts are word counts so tests can assert on them."""

    def __init__(self, delay: float = 0.0):
        self._delay = delay

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        reply = f"(fake) You said: {turns[-1].content}"
        words = reply.split(" ")
        for i, word in enumerate(words):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield TextDelta(word if i == 0 else " " + word)
        input_tokens = sum(len(f"{t.context} {t.content}".split()) for t in turns) \
            + len(system.split())
        yield Completion(input_tokens, output_tokens=len(words), stop_reason="end_turn")

    async def complete(self, system: str, prompt: str) -> TextResult:
        words = prompt.split()
        return TextResult(" ".join(words[-6:]), Usage(len(words), min(len(words), 6)))

    async def structured[M: BaseModel](
        self, system: str, prompt: str, schema: type[M]
    ) -> tuple[M, Usage]:
        # Schemas used with the fake provide a deterministic stand-in via `fake(prompt)`.
        fake = getattr(schema, "fake", None)
        if fake is None:
            raise LLMError(f"FakeProvider can't produce {schema.__name__}", retryable=False)
        return fake(prompt), Usage(len(prompt.split()), 5)


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "ollama":
        # Imported here: chat_models builds on the types in this module.
        from app.services.chat_models import LangChainProvider, build_chat_model
        return LangChainProvider(build_chat_model(settings, purpose="main"),
                                 build_chat_model(settings, purpose="aux"))
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    return FakeProvider(delay=0.03 if settings.env == "dev" else 0.0)
