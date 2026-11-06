"""Web search for the agent's `search_web` tool.

Tavily is called over plain HTTP: one endpoint, so no SDK to pin, and tests replace the
transport. Each search spends a Tavily credit (the free plan has 1,000 a month), so the
tool takes from a per-user daily count first (`QuotaService.take_daily`).
"""

from typing import Protocol

import httpx

from app.services.search import SearchHit

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_MAX_QUERY_CHARS = 400  # Tavily rejects longer queries


class WebSearchProvider(Protocol):
    async def search(self, query: str, k: int) -> list[SearchHit]: ...


class TavilySearch:
    def __init__(self, api_key: str, *, timeout_seconds: float,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"})

    async def search(self, query: str, k: int) -> list[SearchHit]:
        r = await self._client.post(TAVILY_URL, json={
            "query": query[:TAVILY_MAX_QUERY_CHARS], "max_results": k, "search_depth": "basic",
        })
        r.raise_for_status()
        return [SearchHit(item["url"], item.get("title") or item["url"], item["url"],
                          item["content"], float(item.get("score") or 0.0))
                for item in r.json().get("results", []) if item.get("content")]

    async def close(self) -> None:
        await self._client.aclose()
