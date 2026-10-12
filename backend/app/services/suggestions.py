"""Suggested follow-up questions shown as clickable chips under an answer.

Model output that ends up in UI must be validated even with structured outputs: the schema
guarantees the JSON shape, not the content. Observed failure modes this guards against:
- control characters and tokenizer artefacts inside strings (e.g. "<ctrl46>" that some
  models emit in Korean text) that break rendering or downstream parsers,
- a JSON fragment or the whole object echoed into a string field,
- empty, duplicated or overlong entries.

Defence in layers: the prompt asks for short questions; the Pydantic validator cleans and
filters what comes back; and any failure (timeout, refusal, unusable output) just means
no suggestions, never a failed reply.
"""

import asyncio
import logging
import re

from pydantic import BaseModel, Field, field_validator

from app.services.llm import ChatTurn, LLMProvider, Usage

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 3
MAX_CHARS = 60

SUGGESTIONS_SYSTEM = (
    f"Suggest up to {MAX_SUGGESTIONS} short follow-up questions the user is likely to ask "
    f"next, given their message and the assistant's answer. Each must be a single question "
    f"under {MAX_CHARS} characters, in the user's language, phrased as the user would type "
    "it. Return an empty list if the exchange was small talk or the user is done."
)

_ARTEFACT_RE = re.compile(r"<ctrl\d+>|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‍﻿]")


class FollowUps(BaseModel):
    questions: list[str] = Field(
        default_factory=list,
        description=f"Up to {MAX_SUGGESTIONS} follow-up questions, or an empty list.",
    )

    @field_validator("questions", mode="before")
    @classmethod
    def _clean(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            text = " ".join(_ARTEFACT_RE.sub("", item).split()).strip(" \"'")
            if not text or len(text) > MAX_CHARS or text[0] in "{[":
                continue
            if (key := text.casefold()) in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        return cleaned[:MAX_SUGGESTIONS]

    @classmethod
    def fake(cls, prompt: str) -> "FollowUps":
        return cls(questions=["더 자세히 설명해줘", "예시를 들어줄래?"])


class SuggestionService:
    def __init__(self, llm: LLMProvider, timeout_seconds: float):
        self._llm = llm
        self._timeout = timeout_seconds

    async def generate(self, turns: list[ChatTurn], answer: str) -> tuple[list[str], Usage]:
        prompt = f"User: {turns[-1].content[:1000]}\n\nAssistant: {answer[:2000]}"
        try:
            async with asyncio.timeout(self._timeout):
                result, usage = await self._llm.structured(SUGGESTIONS_SYSTEM, prompt, FollowUps)
        except TimeoutError:
            log.warning("follow-up suggestions timed out")
            return [], Usage()
        except Exception:
            log.warning("follow-up suggestions failed", exc_info=True)
            return [], Usage()
        # Drop suggestions that just repeat what the user already asked.
        asked = turns[-1].content.strip().casefold()
        return [q for q in result.questions if q.casefold() != asked], usage
