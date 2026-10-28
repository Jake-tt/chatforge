"""LLM types and the auxiliary-call interface.

The agent answers with LangChain chat models (`services.chat_models`). The short auxiliary
calls (routing, source compression, suggestions, titles, summaries) go through
`LLMProvider`: `LangChainProvider` over the same models for real providers, and the
deterministic `FakeProvider` for local runs without a model and for tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

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
    async def complete(self, system: str, prompt: str) -> TextResult: ...

    async def structured[M: BaseModel](
        self, system: str, prompt: str, schema: type[M]
    ) -> tuple[M, Usage]:
        """Return an instance of `schema`; raise LLMError if the model can't produce one."""
        ...


class FakeProvider:
    """Deterministic aux calls. Token counts are word counts so tests can assert on them."""

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
    if settings.llm_provider in ("ollama", "anthropic"):
        # Imported here: chat_models builds on the types in this module.
        from app.services.chat_models import LangChainProvider, build_chat_model
        return LangChainProvider(build_chat_model(settings, purpose="aux"),
                                 structured_method="json_schema")
    return FakeProvider()
