"""An agent evaluation run as a Langfuse experiment (audit A2, decision D6).

The summary table stays in the repository; the per-case detail goes to a local Langfuse:
the cases are the items of one dataset, and each run is an experiment (dataset run) whose
items carry the case's trace and its scores.

The SDK's `run_experiment` does the linking: a Langfuse v4 server recognizes an experiment
by attributes the SDK puts on each item's spans, not by dataset run items alone. It runs
its tasks on an event loop of its own, while the app (database engine, lifespan) lives on
the runner's loop, so the experiment runs on a worker thread and each task hands its case
back to the runner's loop.

Each case's requests carry the task span as a W3C `traceparent` header: FastAPI starts a
server span per request from the request headers alone, so without it the app's Langfuse
traces (router, agent, tools) would start trace trees of their own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from evals.cases import EvalCase
from evals.scoring import TurnResult, item_scores

if TYPE_CHECKING:
    from langchain_core.callbacks import BaseCallbackHandler

    from app.config import Settings

DATASET = "chatforge-agent-eval"
RunCase = Callable[[EvalCase, dict[str, str]], Awaitable[TurnResult]]


def trace_headers() -> dict[str, str]:
    """The current trace context as request headers (empty outside a span)."""
    from opentelemetry import propagate  # comes with langfuse: .[observability]

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


class LangfuseRun:
    def __init__(self, client: Any, *, run_name: str,
                 handler: BaseCallbackHandler | None = None,
                 metadata: dict[str, str] | None = None,
                 trace_headers: Callable[[], dict[str, str]] = trace_headers):
        self._client = client
        self._trace_headers = trace_headers
        self.run_name = run_name
        self.handler = handler  # traces the app's model calls into `client`
        self._metadata = metadata

    @classmethod
    def from_settings(cls, settings: Settings, *, run_name: str,
                      metadata: dict[str, str] | None = None) -> LangfuseRun:
        from app.tracing import langfuse_client, langfuse_handler

        return cls(langfuse_client(settings), run_name=run_name,
                   handler=langfuse_handler(settings), metadata=metadata)

    @staticmethod
    def item_id(case: EvalCase) -> str:
        # Item ids are unique across a project's datasets.
        return f"{DATASET}:{case.id}"

    def _prepare(self, cases: list[EvalCase]) -> list[Any]:
        """The dataset and its items, upserted (a case edited in the repository updates
        its item), then the items of this run in the cases' order."""
        self._client.create_dataset(
            name=DATASET, description="chatforge agent evaluation cases "
            "(backend/evals/agent_cases.jsonl)")
        for case in cases:
            self._client.create_dataset_item(
                dataset_name=DATASET, id=self.item_id(case),
                input={"turns": case.turns, "setup": case.setup.model_dump()},
                expected_output=case.expect.model_dump(),
                metadata={"category": case.category, "reviewed": case.reviewed})
        items = {item.id: item for item in self._client.get_dataset(DATASET).items}
        return [items[self.item_id(case)] for case in cases]

    async def run(self, cases: list[EvalCase], run_case: RunCase) -> list[TurnResult]:
        """Run every case as one experiment. `run_case` must not raise: a broken case is
        a result with an error, as in a run without Langfuse."""
        loop = asyncio.get_running_loop()
        by_item = {self.item_id(case): case for case in cases}
        results: dict[str, TurnResult] = {}

        def task(*, item: Any, **kwargs: Any) -> dict:
            # On the experiment's thread, inside the item's task span.
            case = by_item[item.id]
            result = asyncio.run_coroutine_threadsafe(
                run_case(case, self._trace_headers()), loop).result()
            results[case.id] = result
            return result.as_dict()

        def scores(*, output: dict, **kwargs: Any) -> list[dict]:
            return [{"name": name, "value": value}
                    for name, value in item_scores(TurnResult(**output)).items()]

        def experiment() -> None:
            self._client.run_experiment(
                name=DATASET, run_name=self.run_name, data=self._prepare(cases), task=task,
                evaluators=[scores], max_concurrency=1, metadata=self._metadata)
            self._client.flush()

        await asyncio.to_thread(experiment)
        # A task the SDK never got to (it isolates item failures) runs here, untraced.
        return [results.get(case.id) or await run_case(case, {}) for case in cases]
