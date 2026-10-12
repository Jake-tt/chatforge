"""LLM provider abstraction.

Routes only depend on `LLMProvider`, so the real Anthropic client and the deterministic
`FakeProvider` (local dev without an API key, and tests) are interchangeable.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import anthropic
from pydantic import BaseModel

from app.config import Settings


@dataclass(frozen=True)
class ChatTurn:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completion:
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


StreamEvent = TextDelta | Completion


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens)


@dataclass(frozen=True)
class TextResult:
    text: str
    usage: Usage


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


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


class AnthropicProvider:
    # "default" routes by refusal category, so no fallback model list to maintain.
    _FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings):
        self._client = anthropic.AsyncAnthropic()
        self._model = settings.anthropic_model
        self._aux_model = settings.llm_aux_model  # router, summaries, titles
        self._effort = settings.llm_effort
        self._max_tokens = settings.llm_max_tokens

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        try:
            async with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": t.role, "content": t.content} for t in turns],
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
        yield Completion(
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
            stop_reason=final.stop_reason,
        )

    async def complete(self, system: str, prompt: str) -> TextResult:
        try:
            response = await self._client.messages.create(
                model=self._aux_model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low"},
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc), retryable=True) from exc
        usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
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
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": "low"},
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc), retryable=True) from exc
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise LLMError(f"no structured output (stop_reason={response.stop_reason})",
                           retryable=False)
        return response.parsed_output, Usage(response.usage.input_tokens,
                                             response.usage.output_tokens)


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
        input_tokens = sum(len(t.content.split()) for t in turns) + len(system.split())
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
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    return FakeProvider(delay=0.03 if settings.env == "dev" else 0.0)
