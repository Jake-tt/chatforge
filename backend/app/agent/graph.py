"""The chat agent as a LangGraph state graph.

    router ─▶ master_router ─┬─▶ clarify ─▶ END
                             ├─▶ coordinator ─▶ agent
                             └─▶ agent ⇄ tools ─▶ END

- router: one structured call decides what the turn needs (help center, the user's own
  account, their earlier conversations) or that the request is too ambiguous to act on.
  It records the resulting source plan once, so routing and fetching can't disagree.
- master_router: conditional edge on that plan.
- clarify: the router's clarifying question *is* the reply; no agent call, and the turn
  is excluded from summarization (folding it away would orphan the user's answer).
- coordinator: fetches everything in the plan concurrently and attaches it to the
  current turn as numbered sources.
- agent: a ReAct loop over tools for anything the prefetch didn't cover. If the router
  failed, the agent gets the tools and decides itself rather than answering blind.

Failure handling carried over from production: a malformed tool call is retried once with
no tools bound (re-asking with tools bound just repeats the bad call); tool rounds and
tool calls per round are capped; every model call has a timeout.
"""

import asyncio
import dataclasses
import logging
import operator
import time
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import Field, field_validator

from app.agent.tools import Tool, ToolOutput
from app.services.chat_models import (
    scratch_messages,
    stop_reason_of,
    to_messages,
    usage_from_message,
)
from app.services.llm import ChatTurn, Usage
from app.services.rag import ROUTER_SYSTEM, PastSearch, RagPipeline, RouteDecision
from app.services.search import SearchHit

log = logging.getLogger(__name__)

CLARIFY_MAX_CHARS = 200

AGENT_ROUTER_SYSTEM = ROUTER_SYSTEM + (
    " Set `needs_clarification` only when the latest message cannot be answered or searched "
    "without asking what the user means, for example a bare pronoun with nothing in the "
    "conversation it could refer to; then write one short clarifying question in the "
    "user's language in `clarifying_question`."
)

TOOLS_NOTE = """

You can call tools to look up the help center, this user's account, and their earlier \
conversations. Call one only when the sources you already have don't answer the question. \
Do not write any text in a message that calls tools. Cite sources inline by number, like [1]."""

# Sent with the current turn, not the system prompt, so the cached prefix stays the same.
ROUTE_FAILED_NOTE = "(This request could not be routed automatically. Decide yourself " \
    "whether a tool is needed.)"


class AgentRoute(RouteDecision):
    needs_clarification: bool = Field(
        default=False, description="True if the request is too ambiguous to act on.")
    clarifying_question: str = Field(default="", description="One short question.")

    @field_validator("clarifying_question")
    @classmethod
    def _trim(cls, value: str) -> str:
        # Local models don't always honour length limits in the schema; a long question is
        # still a question, so trim it rather than failing the whole route.
        return value.strip()[:CLARIFY_MAX_CHARS]

    @classmethod
    def fake(cls, prompt: str) -> "AgentRoute":
        base = RouteDecision.fake(prompt)
        latest = prompt.rsplit("Latest user message:", 1)[-1].strip()
        vague = latest.startswith(("그거", "이거")) and len(latest) < 15
        return cls(**base.model_dump(), needs_clarification=vague,
                   clarifying_question="어떤 것을 말씀하시는지 조금 더 알려주시겠어요?"
                   if vague else "")


@dataclass(frozen=True)
class AgentContext:
    """Per-request dependencies, passed as the graph's runtime context."""

    model: BaseChatModel
    rag: RagPipeline
    tools: list[Tool]
    system: str
    account: SearchHit | None
    past: PastSearch | None
    max_tool_rounds: int
    max_tool_calls: int
    timeout_seconds: float
    prompt_cache: bool  # mark cache breakpoints (Anthropic)


@dataclass(frozen=True)
class SourcePlan:
    """What the coordinator fetches; decided once by the router node."""

    query: str
    help: bool
    account: bool
    past: bool

    @property
    def any(self) -> bool:
        return self.help or self.account or self.past


class AgentState(TypedDict, total=False):
    turns: list[ChatTurn]  # history + current turn; the current one gains `context`
    route: AgentRoute | None
    plan: SourcePlan | None
    citations: Annotated[list[dict], operator.add]
    scratch: Annotated[list[BaseMessage], operator.add]  # tool calls and results
    tool_rounds: int
    usage: Annotated[Usage, operator.add]
    answer: str
    stop_reason: str
    clarified: bool


def plan_sources(route: AgentRoute, turns: list[ChatTurn], ctx: AgentContext) -> SourcePlan:
    return SourcePlan(
        query=route.query.strip() or turns[-1].content,
        help=route.needs_search or ctx.rag.hybrid(turns, route.query),
        account=route.needs_account and ctx.account is not None,
        past=route.needs_past and ctx.past is not None,
    )


async def router(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    started = time.perf_counter()
    route, used = await ctx.rag.classify(state["turns"], AGENT_ROUTER_SYSTEM, AgentRoute)
    plan = plan_sources(route, state["turns"], ctx) if route is not None else None
    log.info("agent route", extra={
        "routed": route is not None,
        "clarify": bool(route and route.needs_clarification),
        "plan": dataclasses.asdict(plan) if plan else None,
        "router_ms": round((time.perf_counter() - started) * 1000),
    })
    return {"route": route, "plan": plan, "usage": used}


def master_router(state: AgentState) -> Literal["clarify", "coordinator", "agent"]:
    route, plan = state.get("route"), state.get("plan")
    if route is None:
        return "agent"  # router failed: the agent picks tools itself
    if route.needs_clarification and route.clarifying_question:
        return "clarify"
    return "coordinator" if plan is not None and plan.any else "agent"


async def clarify(state: AgentState) -> dict:
    return {"answer": state["route"].clarifying_question, "clarified": True,
            "stop_reason": "end_turn"}


async def coordinator(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx, plan, turns = runtime.context, state["plan"], state["turns"]
    result = await ctx.rag.gather(
        plan.query, search_help=plan.help,
        account=ctx.account if plan.account else None,
        past=ctx.past if plan.past else None,
    )
    if result.grounding is None:
        return {"usage": result.usage}
    current = dataclasses.replace(turns[-1], context=result.grounding.context)
    return {"turns": [*turns[:-1], current], "usage": result.usage,
            "citations": [c.as_dict() for c in result.grounding.citations]}


def _agent_turns(state: AgentState) -> list[ChatTurn]:
    turns = state["turns"]
    if state.get("route") is not None:
        return turns
    current = turns[-1]
    note = f"{current.context}\n\n{ROUTE_FAILED_NOTE}" if current.context else ROUTE_FAILED_NOTE
    return [*turns[:-1], dataclasses.replace(current, context=note)]


async def agent(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    # The answering round keeps the breakpoint too: a cache is read only at a breakpoint
    # (or a few blocks before one), so without it this round would resend every earlier
    # tool result uncached to save a write premium on the newest one.
    messages = to_messages(ctx.system + TOOLS_NOTE, _agent_turns(state),
                           cache=ctx.prompt_cache) + \
        scratch_messages(state.get("scratch", []), cache=ctx.prompt_cache)
    can_use_tools = bool(ctx.tools) and state.get("tool_rounds", 0) < ctx.max_tool_rounds
    model = ctx.model.bind_tools([t.as_openai() for t in ctx.tools]) if can_use_tools \
        else ctx.model
    async with asyncio.timeout(ctx.timeout_seconds):
        reply = await model.ainvoke(messages)
        used = usage_from_message(reply)
        if reply.invalid_tool_calls and not reply.tool_calls:
            log.warning("malformed tool call; retrying without tools",
                        extra={"errors": [c.get("error") for c in reply.invalid_tool_calls]})
            reply = await ctx.model.ainvoke(messages)
            used += usage_from_message(reply)
    if reply.tool_calls and can_use_tools:
        return {"scratch": [reply], "usage": used}
    return {"answer": reply.text, "usage": used, "stop_reason": stop_reason_of(reply)}


async def tools(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    by_name = {t.name: t for t in ctx.tools}
    calls = state["scratch"][-1].tool_calls
    allowed, refused = calls[:ctx.max_tool_calls], calls[ctx.max_tool_calls:]
    # The model may ask for several lookups at once; they're independent, so run together.
    outputs = await asyncio.gather(*(_run_tool(by_name, c) for c in allowed))
    outputs += [ToolOutput.failed(f"too many tool calls; at most {ctx.max_tool_calls} "
                                  "per turn")] * len(refused)
    citations, results = number_results(len(state.get("citations", [])) + 1, calls, outputs)
    return {"scratch": results, "citations": citations,
            "tool_rounds": state.get("tool_rounds", 0) + 1}


def number_results(first: int, calls: list[dict], outputs: list[ToolOutput]
                   ) -> tuple[list[dict], list[ToolMessage]]:
    """Number each tool's hits after the sources already cited, in call order."""
    citations: list[dict] = []
    messages: list[ToolMessage] = []
    n = first
    for call, output in zip(calls, outputs, strict=True):
        numbered = [{**c, "n": n + i} for i, c in enumerate(output.citations)]
        n += len(numbered)
        citations += numbered
        messages.append(ToolMessage(output.render(numbered), tool_call_id=call["id"],
                                    status="error" if output.error else "success"))
    return citations, messages


async def _run_tool(by_name: dict[str, Tool], call: dict) -> ToolOutput:
    tool = by_name.get(call["name"])
    if tool is None:
        return ToolOutput.failed(f"unknown tool {call['name']}")
    try:
        return await tool.run(call.get("args") or {})
    except Exception as exc:
        log.warning("tool %s failed", call["name"], exc_info=True)
        return ToolOutput.failed(f"{type(exc).__name__}: the lookup failed")


def after_agent(state: AgentState) -> Literal["tools", "__end__"]:
    scratch = state.get("scratch", [])
    pending = scratch and isinstance(scratch[-1], AIMessage) and scratch[-1].tool_calls
    return "tools" if pending and "answer" not in state else END


def build_agent_graph():
    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("router", router)
    graph.add_node("clarify", clarify)
    graph.add_node("coordinator", coordinator)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", master_router)
    graph.add_edge("clarify", END)
    graph.add_edge("coordinator", "agent")
    graph.add_conditional_edges("agent", after_agent)
    graph.add_edge("tools", "agent")
    return graph.compile()
