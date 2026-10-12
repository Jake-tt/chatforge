"""LLM provider abstraction.

Routes only depend on `LLMProvider`, so the real Anthropic client and the deterministic
`FakeProvider` (local dev without an API key, and tests) are interchangeable.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import anthropic

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


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        """Yield TextDelta chunks, then exactly one Completion."""
        ...

    async def complete(self, system: str, prompt: str) -> str: ...


class AnthropicProvider:
    # "default" routes by refusal category, so no fallback model list to maintain.
    _FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings):
        self._client = anthropic.AsyncAnthropic()
        self._model = settings.anthropic_model
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

    async def complete(self, system: str, prompt: str) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low"},
        )
        if response.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in response.content if b.type == "text").strip()


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

    async def complete(self, system: str, prompt: str) -> str:
        return " ".join(prompt.split()[-6:])


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    return FakeProvider(delay=0.03 if settings.env == "dev" else 0.0)
