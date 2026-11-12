"""Agent evaluation cases (`agent_cases.jsonl`, one JSON object per line).

A case is a short conversation whose last user message is scored: the routing the router
should choose for it and the facts its answer should contain. `setup` is what the member
already has before the conversation starts (earlier conversations, remembered facts).

Labels are the answer key, so a case counts toward a baseline only once a person has
reviewed it (`reviewed`): cases drafted by the same assistant that is evaluated would
otherwise grade themselves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["help", "account", "past", "memory", "clarify", "chitchat"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemorySeed(_Strict):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)


class Setup(_Strict):
    # Each earlier conversation as the member's own messages, oldest first.
    past: list[list[str]] = Field(default_factory=list)
    memory: list[MemorySeed] = Field(default_factory=list)


class Expect(_Strict):
    needs_search: bool = False
    needs_account: bool = False
    needs_past: bool = False
    needs_clarification: bool = False
    # Each group lists the ways one fact may be written; any one of them counts.
    facts: list[list[str]] = Field(default_factory=list)

    @field_validator("facts")
    @classmethod
    def _groups_have_spellings(cls, facts: list[list[str]]) -> list[list[str]]:
        if any(not group or not all(s.strip() for s in group) for group in facts):
            raise ValueError("every fact group needs at least one non-empty spelling")
        return facts


class EvalCase(_Strict):
    id: str = Field(min_length=1)
    category: Category
    turns: list[str] = Field(min_length=1)
    setup: Setup = Field(default_factory=Setup)
    expect: Expect = Field(default_factory=Expect)
    reviewed: bool = False
    note: str = ""  # why the labels are what they are, for the reviewer


def load_cases(path: Path) -> list[EvalCase]:
    cases = [EvalCase.model_validate(json.loads(line))
             for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
    return cases
