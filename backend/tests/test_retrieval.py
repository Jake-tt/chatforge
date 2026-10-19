"""Parallel retrieval: independent sources run concurrently, each isolated by its own
timeout, and their results come back in source order."""

import asyncio
import time

from app.services.retrieval import SourceSpec, gather_sources
from app.services.search import SearchHit


def _hit(doc_id: str) -> SearchHit:
    return SearchHit(doc_id, doc_id, f"/{doc_id}", f"text of {doc_id}", 1.0)


async def _sleep_then(seconds: float, hits: list[SearchHit]) -> list[SearchHit]:
    await asyncio.sleep(seconds)
    return hits


async def test_sources_run_concurrently_and_keep_their_order():
    started = time.perf_counter()

    results = await gather_sources([
        SourceSpec("slow-first", lambda: _sleep_then(0.2, [_hit("a")]), timeout=1.0),
        SourceSpec("fast-second", lambda: _sleep_then(0.1, [_hit("b")]), timeout=1.0),
    ])

    assert time.perf_counter() - started < 0.3  # the slowest source, not the sum
    # Source order, not completion order, so citation numbers are deterministic.
    assert [(r.name, [h.doc_id for h in r.hits]) for r in results] == [
        ("slow-first", ["a"]), ("fast-second", ["b"]),
    ]


async def test_a_slow_or_failing_source_only_loses_its_own_results():
    async def broken() -> list[SearchHit]:
        raise ConnectionError("search backend down")

    results = await gather_sources([
        SourceSpec("slow", lambda: _sleep_then(1.0, [_hit("s")]), timeout=0.05),
        SourceSpec("broken", broken, timeout=1.0),
        SourceSpec("ok", lambda: _sleep_then(0.0, [_hit("o")]), timeout=1.0),
    ])

    assert [(r.name, r.error, len(r.hits)) for r in results] == [
        ("slow", "timeout", 0), ("broken", "ConnectionError", 0), ("ok", None, 1),
    ]
    # The exception itself is kept so the caller can log its message and traceback.
    assert str(results[1].exc) == "search backend down"
