"""Parallel retrieval over independent sources.

A turn can need several kinds of context at once, e.g. the help center and the user's own
earlier conversations. Sources don't depend on each other, so they run concurrently and the
wait is the slowest source, not the sum of all of them.

Each source is isolated: its own timeout, and any exception is caught and kept with its
result. A slow or broken source only loses its own results. Results come back in the order
the sources were given, not completion order, so citation numbers are deterministic.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.services.search import SearchHit

Fetch = Callable[[], Awaitable[list[SearchHit]]]


@dataclass(frozen=True)
class SourceSpec:
    name: str
    fetch: Fetch
    timeout: float


@dataclass(frozen=True)
class SourceResult:
    name: str
    hits: list[SearchHit] = field(default_factory=list)
    error: str | None = None  # "timeout" or the exception class name
    exc: BaseException | None = None  # for logging the message and traceback
    ms: int = 0


async def _run(spec: SourceSpec) -> SourceResult:
    started = time.perf_counter()

    def elapsed() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        async with asyncio.timeout(spec.timeout):
            hits = await spec.fetch()
    except TimeoutError as exc:
        return SourceResult(spec.name, error="timeout", exc=exc, ms=elapsed())
    except Exception as exc:
        return SourceResult(spec.name, error=type(exc).__name__, exc=exc, ms=elapsed())
    return SourceResult(spec.name, hits, ms=elapsed())


async def gather_sources(specs: list[SourceSpec]) -> list[SourceResult]:
    # _run never raises, so one failure can't cancel the others.
    return list(await asyncio.gather(*(_run(s) for s in specs)))
