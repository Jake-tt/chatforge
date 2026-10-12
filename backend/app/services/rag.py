"""Retrieval-augmented answers from the help-center knowledge base.

Per chat turn:
1. Route   — a small structured LLM call decides whether the turn needs the knowledge base
             and rewrites it into a standalone query ("그럼 프로는?" → "프로 요금제 일일 토큰
             한도"). Routing is an optimisation, so a timeout or any failure falls back to
             answering without search instead of failing the chat.
2. Hybrid  — from the Nth user turn on, search whenever the router produced a query even if
             it judged search unnecessary: follow-ups deep in a conversation often need
             facts the router underrates.
3. Search  — retrieval with its own timeout.
4. Ground  — sources are numbered and injected into the system prompt. If they exceed the
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
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field

from app.config import Settings
from app.services.llm import ChatTurn, LLMProvider, Usage
from app.services.search import SearchHit, SearchProvider

log = logging.getLogger(__name__)

ROUTER_SYSTEM = (
    "You route messages for the Chatforge help assistant. The knowledge base covers "
    "Chatforge plans and daily token limits, accounts and login, moving guest chats into an "
    "account, deleting conversations, privacy, and chat features. Decide whether answering "
    "the latest user message needs that knowledge base. Always fill `query` with a "
    "standalone search query in the user's language that resolves references to earlier "
    "turns; leave it empty only for pure small talk."
)

SUMMARY_SYSTEM = (
    "Condense the sources into at most {limit} characters of facts that help answer the "
    "question. Keep every fact attached to its source number in square brackets, like [2]. "
    "Do not add anything that is not in the sources. Write in the language of the sources."
)

GROUNDING_TEMPLATE = """

Use the help-center sources below when they are relevant. Cite them inline by number, \
like [1]. If they don't cover the question, say you don't know instead of guessing.

<sources>
{body}
</sources>"""

_FAKE_KEYWORDS = ("요금", "플랜", "한도", "토큰", "계정", "가입", "로그인", "삭제", "비밀번호",
                  "plan", "limit", "account")
_MARKER_RE = re.compile(r"\[(\d+)\]")


class RouteDecision(BaseModel):
    needs_search: bool = Field(description="True if the answer needs the knowledge base.")
    query: str = Field(default="", max_length=200,
                       description="Standalone search query, or empty for small talk.")

    @classmethod
    def fake(cls, prompt: str) -> "RouteDecision":
        """Deterministic stand-in used by FakeProvider (dev without an API key, tests)."""
        latest = prompt.rsplit("Latest user message:", 1)[-1].strip()
        needs = "?" in latest or any(k in latest.lower() for k in _FAKE_KEYWORDS)
        return cls(needs_search=needs, query=latest[:200])


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
    context: str  # appended to the system prompt
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

    async def ground(self, turns: list[ChatTurn]) -> GroundingResult:
        usage = Usage()
        started = time.perf_counter()
        decision, route_usage = await self._route(turns)
        usage += route_usage
        user_turns = sum(t.role == "user" for t in turns)
        hybrid = user_turns >= self._s.search_after_turns and bool(decision.query.strip())
        log.info("rag route", extra={
            "needs_search": decision.needs_search, "hybrid": hybrid, "query": decision.query,
            "router_ms": round((time.perf_counter() - started) * 1000),
        })
        if not (decision.needs_search or hybrid):
            return GroundingResult(None, usage)

        query = decision.query.strip() or turns[-1].content
        hits = await self._retrieve(query)
        if not hits:
            return GroundingResult(None, usage)

        citations = [Citation(i, h.title, h.url, h.text) for i, h in enumerate(hits, start=1)]
        body = _render(citations)
        compressed = False
        if len(body) > self._s.grounding_char_budget:
            summary, summary_usage = await self._compress(query, body, len(citations))
            usage += summary_usage
            if summary:
                body, compressed = summary, True
            else:
                body = _render(citations, per_source=self._s.grounding_char_budget
                               // len(citations))
        return GroundingResult(
            Grounding(citations, GROUNDING_TEMPLATE.format(body=body), compressed), usage
        )

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

    async def _retrieve(self, query: str) -> list[SearchHit]:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._s.search_timeout_seconds):
                hits = await self._search.search(query, self._s.search_top_k)
        except Exception:
            log.warning("rag search failed; answering without sources", exc_info=True)
            return []
        log.info("rag search", extra={
            "query": query, "hits": len(hits),
            "search_ms": round((time.perf_counter() - started) * 1000),
        })
        return hits

    async def _compress(self, query: str, body: str, n_sources: int) -> tuple[str | None, Usage]:
        limit = self._s.summary_char_limit
        try:
            async with asyncio.timeout(self._s.router_timeout_seconds):
                result = await self._llm.complete(
                    SUMMARY_SYSTEM.format(limit=limit), f"Question: {query}\n\nSources:\n{body}"
                )
        except Exception:
            log.warning("source compression failed; using truncated sources", exc_info=True)
            return None, Usage()
        if not _keeps_citations(result.text, n_sources, limit):
            log.warning("compressed sources dropped citations; using truncated sources")
            return None, result.usage
        return result.text, result.usage


def _render(citations: list[Citation], per_source: int | None = None) -> str:
    def clip(text: str) -> str:
        return text if per_source is None or len(text) <= per_source \
            else text[:per_source].rstrip() + "…"
    return "\n\n".join(f"[{c.n}] {c.title}\n{clip(c.snippet)}" for c in citations)


def _keeps_citations(summary: str, n_sources: int, limit: int) -> bool:
    """A usable summary is non-empty, within ~limit, cites at least one source, and only
    cites sources that exist."""
    if not summary.strip() or len(summary) > limit * 1.2:
        return False
    cited = {int(m) for m in _MARKER_RE.findall(summary)}
    return bool(cited) and cited <= set(range(1, n_sources + 1))
