"""Retrieval-augmented answers from the help-center knowledge base.

Per chat turn:
1. Route   — a small structured LLM call decides whether the turn needs the knowledge base
             and rewrites it into a standalone query ("그럼 프로는?" → "프로 요금제 일일 토큰
             한도"). Routing is an optimisation, so a timeout or any failure falls back to
             answering without search instead of failing the chat.
2. Hybrid  — from the Nth user turn on, search whenever the router produced a query even if
             it judged search unnecessary: follow-ups deep in a conversation often need
             facts the router underrates.
3. Search  — the selected sources run concurrently via `retrieval.gather_sources`, each
             with its own timeout; a failed source only loses its own results.
4. Ground  — sources are numbered and sent with the current user turn (not the system
             prompt, which must stay byte-stable for prompt caching). If they exceed the
             character budget they are compressed by the LLM, but the compression must keep
             the [n] citation markers; otherwise it is discarded and the raw sources are
             truncated instead, so a summary can never silently drop attribution.

Tokens spent on routing and compression are returned so the caller can charge them to the
user's quota along with the answer.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from app.config import Settings
from app.services.llm import ChatTurn, LLMProvider, Usage
from app.services.quota import QuotaStatus
from app.services.retrieval import SourceSpec, gather_sources
from app.services.search import SearchHit, SearchProvider

PastSearch = Callable[[str], Awaitable[list[SearchHit]]]

log = logging.getLogger(__name__)

ROUTER_SYSTEM = (
    "You route messages for the Chatforge help assistant. The knowledge base covers "
    "Chatforge plans and daily token limits, accounts and login, moving guest chats into an "
    "account, deleting conversations, privacy, and chat features. Decide whether answering "
    "the latest user message needs that knowledge base. Separately, decide whether it needs "
    "this user's own plan or today's usage, and whether it refers to something from the "
    "user's earlier, separate conversations. Always fill `query` with a "
    "standalone search query in the user's language that resolves references to earlier "
    "turns; leave it empty only for pure small talk."
)

SUMMARY_SYSTEM = (
    "Condense the sources into at most {limit} characters of facts that help answer the "
    "question. Keep every fact attached to its source number in square brackets, like [2]. "
    "Do not add anything that is not in the sources. Write in the language of the sources."
)

GROUNDING_TEMPLATE = """\
Use the sources below when they are relevant: help-center articles, facts about this \
user's own account, and excerpts from their earlier conversations. Cite them inline by \
number, like [1]. If they don't cover the question, say you don't know instead of guessing.

<sources>
{body}
</sources>"""

_FAKE_KEYWORDS = ("요금", "플랜", "한도", "토큰", "계정", "가입", "로그인", "삭제", "비밀번호",
                  "plan", "limit", "account")
# Whole words only: "내 " also occurs inside "안내 ".
_FAKE_ACCOUNT_RE = re.compile(r"(?:^|\s)(?:내|my)\s|남은|remaining")
_FAKE_PAST_RE = re.compile(r"지난번|저번|earlier|last time")
_MARKER_RE = re.compile(r"\[(\d+)\]")
_PLAN_LABELS = {"guest": "게스트", "free": "무료", "pro": "프로"}


class RouteDecision(BaseModel):
    needs_search: bool = Field(description="True if the answer needs the knowledge base.")
    query: str = Field(default="", max_length=200,
                       description="Standalone search query, or empty for small talk.")
    needs_account: bool = Field(
        default=False, description="True if the answer needs this user's plan or usage.")
    needs_past: bool = Field(
        default=False,
        description="True if the user refers to something from an earlier conversation.")

    @classmethod
    def fake(cls, prompt: str) -> "RouteDecision":
        """Deterministic stand-in used by FakeProvider (dev without an API key, tests)."""
        latest = prompt.rsplit("Latest user message:", 1)[-1].strip()
        lowered = latest.lower()
        needs = "?" in latest or any(k in lowered for k in _FAKE_KEYWORDS)
        return cls(needs_search=needs, query=latest[:200],
                   needs_account=bool(_FAKE_ACCOUNT_RE.search(lowered)),
                   needs_past=bool(_FAKE_PAST_RE.search(lowered)))


@dataclass(frozen=True)
class Citation:
    n: int
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Grounding:
    citations: list[Citation]
    context: str  # sent with the current user turn
    compressed: bool


@dataclass(frozen=True)
class GroundingResult:
    grounding: Grounding | None
    usage: Usage  # routing + compression tokens, charged to the user


class RagPipeline:
    def __init__(self, llm: LLMProvider, search: SearchProvider, settings: Settings):
        self._llm = llm
        self._search = search
        self._s = settings

    async def ground(self, turns: list[ChatTurn], account: SearchHit | None = None,
                     past: PastSearch | None = None) -> GroundingResult:
        """`account`: the user's plan and usage. `past`: searches their earlier
        conversations. Each is used only when the router asks for it."""
        decision, route_usage, hybrid = await self.route(turns)
        result = await self.gather(
            decision.query.strip() or turns[-1].content,
            search_help=decision.needs_search or hybrid,
            account=account if decision.needs_account else None,
            past=past if decision.needs_past else None,
        )
        return GroundingResult(result.grounding, route_usage + result.usage)

    async def route(self, turns: list[ChatTurn]) -> tuple[RouteDecision, Usage, bool]:
        """The router's decision, its tokens, and whether hybrid search applies."""
        started = time.perf_counter()
        decision, usage = await self._route(turns)
        user_turns = sum(t.role == "user" for t in turns)
        hybrid = user_turns >= self._s.search_after_turns and bool(decision.query.strip())
        log.info("rag route", extra={
            "needs_search": decision.needs_search, "needs_account": decision.needs_account,
            "needs_past": decision.needs_past, "hybrid": hybrid, "query": decision.query,
            "router_ms": round((time.perf_counter() - started) * 1000),
        })
        return decision, usage, hybrid

    async def gather(self, query: str, *, search_help: bool, account: SearchHit | None,
                     past: PastSearch | None) -> GroundingResult:
        """Run the selected sources concurrently and build the grounding for one turn."""
        pinned = [account] if account is not None else []
        sources: list[SourceSpec] = []
        if search_help:
            sources.append(SourceSpec(
                "help", lambda: self._search.search(query, self._s.search_top_k),
                self._s.search_timeout_seconds))
        if past is not None:
            sources.append(SourceSpec("past", lambda: past(query),
                                      self._s.search_timeout_seconds))
        searched = await self._retrieve(sources) if sources else []
        if not (pinned or searched):
            return GroundingResult(None, Usage())

        citations = [Citation(i, h.title, h.url, h.text)
                     for i, h in enumerate([*pinned, *searched], start=1)]
        # Pinned facts (the user's own figures) are sent verbatim; only search results are
        # compressed, since a summary may reword a number or drop a source.
        searched_body, compressed, usage = await self._searched_body(
            query, citations[len(pinned):])
        body = "\n\n".join(b for b in (_render(citations[:len(pinned)]), searched_body) if b)
        return GroundingResult(
            Grounding(citations, GROUNDING_TEMPLATE.format(body=body), compressed), usage
        )

    async def _searched_body(self, query: str, citations: list[Citation]
                             ) -> tuple[str, bool, Usage]:
        body = _render(citations)
        if len(body) <= self._s.grounding_char_budget:
            return body, False, Usage()
        summary, usage = await self._compress(query, body, {c.n for c in citations})
        if summary:
            return summary, True, usage
        per_source = self._s.grounding_char_budget // len(citations)
        return _render(citations, per_source=per_source), False, usage

    async def _route(self, turns: list[ChatTurn]) -> tuple[RouteDecision, Usage]:
        recent = "\n".join(f"{t.role}: {t.content[:500]}" for t in turns[-6:-1])
        prompt = f"Conversation so far:\n{recent or '(none)'}\n\n" \
                 f"Latest user message: {turns[-1].content}"
        try:
            async with asyncio.timeout(self._s.router_timeout_seconds):
                return await self._llm.structured(ROUTER_SYSTEM, prompt, RouteDecision)
        except TimeoutError:
            log.warning("rag router timed out; answering without search")
        except Exception:
            log.warning("rag router failed; answering without search", exc_info=True)
        return RouteDecision(needs_search=False), Usage()

    async def _retrieve(self, sources: list[SourceSpec]) -> list[SearchHit]:
        started = time.perf_counter()
        results = await gather_sources(sources)
        for r in results:
            if r.exc is not None:
                log.warning("rag source %s failed (%s); answering without it", r.name, r.error,
                            exc_info=r.exc)
        log.info("rag retrieve", extra={
            "sources": {r.name: {"hits": len(r.hits), "ms": r.ms, "error": r.error}
                        for r in results},
            "retrieve_ms": round((time.perf_counter() - started) * 1000),
        })
        # Every source that answered reaches the grounding, in source order.
        return [hit for r in results for hit in r.hits]

    async def _compress(self, query: str, body: str, numbers: set[int]
                        ) -> tuple[str | None, Usage]:
        limit = self._s.summary_char_limit
        try:
            async with asyncio.timeout(self._s.router_timeout_seconds):
                result = await self._llm.complete(
                    SUMMARY_SYSTEM.format(limit=limit), f"Question: {query}\n\nSources:\n{body}"
                )
        except Exception:
            log.warning("source compression failed; using truncated sources", exc_info=True)
            return None, Usage()
        if not _keeps_citations(result.text, numbers, limit):
            log.warning("compressed sources dropped citations; using truncated sources")
            return None, result.usage
        return result.text, result.usage


def _render(citations: list[Citation], per_source: int | None = None) -> str:
    def clip(text: str) -> str:
        return text if per_source is None or len(text) <= per_source \
            else text[:per_source].rstrip() + "…"
    return "\n\n".join(f"[{c.n}] {c.title}\n{clip(c.snippet)}" for c in citations)


def _keeps_citations(summary: str, numbers: set[int], limit: int) -> bool:
    """A usable summary is non-empty, within ~limit, cites at least one source, and only
    cites the sources it was given."""
    if not summary.strip() or len(summary) > limit * 1.2:
        return False
    cited = {int(m) for m in _MARKER_RE.findall(summary)}
    return bool(cited) and cited <= numbers


def account_snapshot(plan: str, status: QuotaStatus) -> SearchHit:
    """The user's plan and today's usage as a citable source.

    Citations are stored with the answer, so the text says when it was taken: reopened
    tomorrow, it must not read as the current balance.
    """
    tz = status.resets_at.tzinfo
    text = (
        f"요금제: {_PLAN_LABELS.get(plan, plan)}\n"
        f"오늘 사용량: {status.used:,} / {status.limit:,} 토큰 (남음 {status.remaining:,})\n"
        f"한도 초기화: {status.resets_at:%Y-%m-%d %H:%M %Z}\n"
        f"기준 시각: {datetime.now(tz):%Y-%m-%d %H:%M %Z}"
    )
    return SearchHit("account", "내 계정 현황", "/usage", text, 1.0)
