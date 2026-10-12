"""Test harness.

Runs against SQLite by default so tests need no services. CI also runs the suite against
Postgres by setting TEST_DATABASE_URL (see .github/workflows/ci.yml).
"""

import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import fakeredis
import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.context import AppContext
from app.db import Base
from app.main import create_app
from app.services.llm import FakeProvider, LLMProvider


@pytest.fixture
def database_url(tmp_path) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def make_settings(database_url) -> Callable[..., Settings]:
    def _make(**overrides) -> Settings:
        # RAG is off unless a test opts in, so chat tests keep exact token counts.
        overrides.setdefault("rag_enabled", False)
        return Settings(env="test", database_url=database_url, _env_file=None, **overrides)
    return _make


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient

    @property
    def ctx(self) -> AppContext:
        return self.app.state.ctx

    async def guest(self) -> dict[str, str]:
        r = await self.client.post("/auth/guest")
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    async def signup(self, email: str, password: str = "password123", **extra) -> httpx.Response:
        return await self.client.post(
            "/auth/signup", json={"email": email, "password": password, **extra}
        )

    async def user(self, email: str = "a@example.com") -> dict[str, str]:
        r = await self.signup(email)
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    async def new_conversation(self, headers) -> str:
        r = await self.client.post("/conversations", json={}, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def chat(self, headers, conversation_id: str, content: str) -> "SSEResult":
        r = await self.client.post(
            f"/conversations/{conversation_id}/messages", json={"content": content},
            headers=headers,
        )
        return SSEResult.parse(r)


@dataclass
class SSEResult:
    response: httpx.Response
    events: list[tuple[str, dict]] = field(default_factory=list)

    @classmethod
    def parse(cls, response: httpx.Response) -> "SSEResult":
        result = cls(response)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for block in response.text.strip().split("\n\n"):
                if block.startswith(":"):
                    continue  # SSE comment (header flush / keep-alive)
                lines = dict(line.split(": ", 1) for line in block.splitlines())
                result.events.append((lines["event"], json.loads(lines["data"])))
        return result

    @property
    def text(self) -> str:
        return "".join(d["text"] for e, d in self.events if e == "delta")

    def last(self, name: str) -> dict:
        return next(d for e, d in reversed(self.events) if e == name)


@pytest.fixture
async def make_harness(make_settings):
    """Factory so a test can tweak settings / swap the LLM or Redis."""
    stack: list = []

    async def _make(
        *, llm: LLMProvider | None = None, redis=None, tasks=None, search=None,
        **settings_overrides,
    ) -> Harness:
        settings = make_settings(**settings_overrides)
        app = create_app(
            settings,
            redis=redis or fakeredis.FakeAsyncRedis(decode_responses=True),
            llm=llm or FakeProvider(),
            tasks=tasks,
            search=search,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        stack.append(lifespan)
        async with app.state.ctx.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        stack.append(client)
        return Harness(app, client)

    yield _make

    for item in reversed(stack):
        if isinstance(item, httpx.AsyncClient):
            await item.aclose()
        else:
            await item.__aexit__(None, None, None)


@pytest.fixture
async def h(make_harness) -> AsyncIterator[Harness]:
    yield await make_harness()
