"""Agent evaluation runner (audit A2): the real path, case by case.

    cd backend && LLM_PROVIDER=ollama python -m evals.run_agent_eval [--only help] [--limit 5]

Each case runs through the app's HTTP API in-process (sign up, ask, read the SSE stream)
with the configured models, so routing, tools, storage and billing are what production
runs. Only the case's last message is scored. What a member already has (earlier
conversations, remembered facts) is put in the database directly: there is no API that
adds a memory, and having a model write the earlier conversations would make the setup
differ run to run.

Measured per scored turn: the router's decision (its `agent route` log record), LLM calls
(a callback counting model runs, attached where tracing goes), `billed_tokens` (`done`),
time to the first `delta` and to the end of the stream. An unscored turn runs first so a
local model's cold start isn't measured as the first case.

Not in CI: real models are slow and not deterministic. LLM_PROVIDER=anthropic costs money,
so it needs --allow-cost (the repository rule: estimate and approve first). Results go to
`evals/results/` (not committed); the summary table is copied into `plan/` by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from sqlalchemy import update

from app.config import Settings, get_settings
from app.db import Base
from app.main import create_app
from app.models import Conversation, Message
from app.observability import request_id_var
from app.services import memory
from app.services.llm import LLMProvider
from evals.cases import EvalCase, load_cases
from evals.scoring import FLAGS, TurnResult, fact_hits, render_markdown, summarize

CASES = Path(__file__).with_name("agent_cases.jsonl")
RESULTS = Path(__file__).with_name("results")
PASSWORD = "eval-password-123"


class _NoTasks:
    """Background work (titles, summaries, memory extraction) isn't part of a turn."""

    async def enqueue(self, name: str, payload: dict, *, delay_seconds: float = 0) -> None:
        pass

    async def close(self) -> None:
        pass


class _CallCounter(BaseCallbackHandler):
    """Model runs per request, from the tracing hook (request_id is in each run's metadata)."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def _count(self, metadata: dict | None) -> None:
        if metadata and metadata.get("request_id"):
            self.calls[metadata["request_id"]] += 1

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        self._count(metadata)

    def on_llm_start(self, serialized, prompts, *, metadata=None, **kwargs):
        self._count(metadata)


class _RouteLog(logging.Handler):
    """The router's decision per request: what it planned to look up and whether it asked
    back (the `agent route` record in app.agent.graph)."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, dict | None] = {}

    def emit(self, record: logging.LogRecord) -> None:
        rid = request_id_var.get()
        if record.getMessage() != "agent route" or rid is None:
            return
        plan = getattr(record, "plan", None)
        self.routes[rid] = None if not getattr(record, "routed", False) else {
            "needs_search": bool(plan and plan["help"]),
            "needs_account": bool(plan and plan["account"]),
            "needs_past": bool(plan and plan["past"]),
            "needs_clarification": bool(getattr(record, "clarify", False)),
        }


def _eval_settings(settings: Settings) -> Settings:
    # The counter rides on the tracing hook; a real Langfuse isn't involved, so the keys
    # are placeholders (model_copy skips the check that would ask for real ones). Each case
    # signs up a fresh member, so the per-IP limits are lifted.
    return settings.model_copy(update={
        "langfuse_enabled": True, "signups_per_hour_per_ip": 100_000,
        "requests_per_minute": 100_000, "task_backend": "inline"})


async def _seed(app, user_id: uuid.UUID, headers: dict, client: httpx.AsyncClient,
                case: EvalCase) -> None:
    ctx = app.state.ctx
    async with ctx.sessionmaker() as session:
        for messages in case.setup.past:
            cid = uuid.UUID((await client.post("/conversations", headers=headers,
                                               json={})).json()["id"])
            for seq, content in enumerate(messages, start=1):
                session.add(Message(conversation_id=cid, seq=seq, role="user",
                                    content=content))
            await session.execute(update(Conversation).where(Conversation.id == cid).values(
                message_count=len(messages), title=messages[0][:60]))
        for fact in case.setup.memory:
            await memory.add_fact(session, user_id, fact.subject, fact.predicate,
                                  value=fact.object, conversation_id=cid if case.setup.past
                                  else await _memory_source(client, headers))
        await session.commit()


async def _memory_source(client: httpx.AsyncClient, headers: dict) -> uuid.UUID:
    """A remembered fact records the conversation it came from."""
    return uuid.UUID((await client.post("/conversations", headers=headers,
                                        json={})).json()["id"])


async def _ask(client: httpx.AsyncClient, headers: dict, cid: str, content: str) -> dict:
    events: list[tuple[str, dict]] = []
    started = time.perf_counter()
    first_delta = None
    async with client.stream("POST", f"/conversations/{cid}/messages", headers=headers,
                             json={"content": content}, timeout=None) as response:
        if response.status_code != 200:
            return {"events": [("error", {"message": f"HTTP {response.status_code}"})],
                    "first_delta_ms": None, "total_ms": 0.0}
        name = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                if name == "delta" and first_delta is None:
                    first_delta = (time.perf_counter() - started) * 1000
                events.append((name, json.loads(line[6:])))
                name = None
    return {"events": events, "first_delta_ms": first_delta,
            "total_ms": (time.perf_counter() - started) * 1000}


async def _run_case(app, client: httpx.AsyncClient, counter: _CallCounter, routes: _RouteLog,
                    case: EvalCase) -> TurnResult:
    signup = await client.post("/auth/signup", json={
        "email": f"eval-{uuid.uuid4().hex[:12]}@example.com", "password": PASSWORD})
    body = signup.json()
    headers = {"Authorization": f"Bearer {body['token']}"}
    await _seed(app, uuid.UUID(body["user"]["id"]), headers, client, case)
    cid = (await client.post("/conversations", headers=headers, json={})).json()["id"]

    turn = None
    rid = ""
    for i, content in enumerate(case.turns):
        rid = f"{case.id}-{i}-{uuid.uuid4().hex[:6]}"
        turn = await _ask(client, {**headers, "X-Request-ID": rid}, cid, content)
    events = turn["events"]
    by_name: dict[str, list[dict]] = {}
    for name, data in events:
        by_name.setdefault(name, []).append(data)
    answer = "".join(d["text"] for d in by_name.get("delta", []))
    sources = [c["n"] for c in (by_name.get("sources") or [{"citations": []}])[-1]["citations"]]
    done = (by_name.get("done") or [{}])[-1]
    error = (by_name.get("error") or [{}])[-1].get("message") if "error" in by_name else None
    hits, total = fact_hits(answer, case.expect.facts)
    return TurnResult(
        case_id=case.id, category=case.category, reviewed=case.reviewed,
        expected={f: getattr(case.expect, f) for f in FLAGS},
        predicted=routes.routes.get(rid), clarified="clarify" in by_name, answer=answer,
        sources=sources, fact_hits=hits, fact_total=total, llm_calls=counter.calls[rid],
        billed_tokens=done.get("billed_tokens", 0), first_delta_ms=turn["first_delta_ms"],
        total_ms=turn["total_ms"], error=error)


async def _warm_up(client: httpx.AsyncClient) -> None:
    """One unscored turn first. The app loads a local model in the background at startup,
    and a router call made while it loads times out: the first case would measure a cold
    start, not the agent."""
    headers = {"Authorization": f"Bearer {(await client.post('/auth/guest')).json()['token']}"}
    cid = (await client.post("/conversations", headers=headers, json={})).json()["id"]
    await _ask(client, headers, cid, "안녕하세요")


async def run_cases(cases: list[EvalCase], settings: Settings, *,
                    llm: LLMProvider | None = None,
                    chat_model: BaseChatModel | None = None) -> list[TurnResult]:
    """Run each case on a fresh app; `llm`/`chat_model` replace the configured models."""
    counter, routes = _CallCounter(), _RouteLog()
    route_logger = logging.getLogger("app.agent.graph")
    route_logger.addHandler(routes)
    previous_level = route_logger.level
    route_logger.setLevel(logging.INFO)
    app = create_app(_eval_settings(settings), redis=fakeredis.FakeAsyncRedis(
        decode_responses=True), llm=llm, tasks=_NoTasks(), chat_model=chat_model,
        tracing_handler=counter)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        async with app.state.ctx.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://eval") as client:
            await _warm_up(client)
            results = []
            for case in cases:
                try:
                    results.append(await _run_case(app, client, counter, routes, case))
                except Exception as exc:  # one broken case shouldn't end the run
                    results.append(TurnResult(
                        case_id=case.id, category=case.category, reviewed=case.reviewed,
                        expected={f: getattr(case.expect, f) for f in FLAGS}, predicted=None,
                        clarified=False, answer="", sources=[], fact_hits=0,
                        fact_total=len(case.expect.facts), llm_calls=0, billed_tokens=0,
                        first_delta_ms=None, total_ms=0.0, error=repr(exc)))
            return results
    finally:
        await lifespan.__aexit__(None, None, None)
        route_logger.removeHandler(routes)
        route_logger.setLevel(previous_level)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--only", help="run one category (help, account, past, ...)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--allow-cost", action="store_true",
                        help="required with LLM_PROVIDER=anthropic (it is billed)")
    args = parser.parse_args()

    cases = [c for c in load_cases(args.cases) if not args.only or c.category == args.only]
    cases = cases[:args.limit] if args.limit else cases
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    RESULTS.mkdir(exist_ok=True)
    base = get_settings()
    if base.llm_provider == "anthropic" and not args.allow_cost:
        raise SystemExit("LLM_PROVIDER=anthropic is billed: estimate the cost, get it "
                         "approved, then pass --allow-cost")
    settings = base.model_copy(update={
        "database_url": f"sqlite+aiosqlite:///{RESULTS / f'{stamp}.db'}"})

    results = asyncio.run(run_cases(cases, settings))
    summary = summarize(results)
    title = f"에이전트 평가 — {base.llm_provider} {stamp}"
    (RESULTS / f"{stamp}.json").write_text(json.dumps(
        {"provider": base.llm_provider, "summary": summary,
         "results": [r.as_dict() for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    markdown = render_markdown(summary, title)
    (RESULTS / f"{stamp}.md").write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
