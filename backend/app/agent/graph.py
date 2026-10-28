"""The chat agent as a LangGraph state graph.

    router ─▶ master_router ─┬─▶ clarify ─▶ END
                             ├─▶ coordinator ─▶ agent
                             └─▶ agent ⇄ tools ─▶ END

- router: one structured call decides what the turn needs (help center, the user's own
  account, their earlier conversations) or that the request is too ambiguous to act on.
- master_router: conditional edge on that decision.
- clarify: the router's clarifying question *is* the reply; no agent call, and the turn
  is excluded from summarization (folding it away would orphan the user's answer).
- coordinator: fetches everything the router asked for concurrently and attaches it to the
  current turn as numbered sources.
- agent: a ReAct loop over tools for anything the prefetch didn't cover. If the router
  failed, the agent gets the tools and decides itself rather than answering blind.

Failure handling carried over from production: a malformed tool call is retried once with
no tools bound (re-asking with tools bound just repeats the bad call), and tool rounds are
capped, after which the model must answer with what it has.
"""

import asyncio
import dataclasses
import logging
import operator
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import Field

from app.agent.tools import Tool, ToolOutput
from app.services.chat_models import to_messages, usage_from_message
from app.services.llm import ChatTurn, Usage
from app.services.rag import ROUTER_SYSTEM, PastSearch, RagPipeline, RouteDecision
from app.services.search import SearchHit

log = logging.getLogger(__name__)

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

ROUTE_FAILED_NOTE = """

This request could not be routed automatically. Decide yourself whether a tool is needed."""


class AgentRoute(RouteDecision):
    needs_clarification: bool = Field(
        default=False, description="True if the request is too ambiguous to act on.")
    clarifying_question: str = Field(default="", max_length=200)

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


class AgentState(TypedDict, total=False):
    turns: list[ChatTurn]  # history + current turn; the current one gains `context`
    route: AgentRoute | None
    citations: Annotated[list[dict], operator.add]
    scratch: Annotated[list[BaseMessage], operator.add]  # tool calls and results
    tool_rounds: int
    usage: Annotated[Usage, operator.add]
    answer: str
    clarified: bool


async def router(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    route, used = await runtime.context.rag.classify(state["turns"], AGENT_ROUTER_SYSTEM,
                                                     AgentRoute)
    return {"route": route, "usage": used}


def master_router(state: AgentState, runtime: Runtime[AgentContext]
                  ) -> Literal["clarify", "coordinator", "agent"]:
    route = state.get("route")
    if route is None:
        return "agent"  # router failed: the agent picks tools itself
    if route.needs_clarification and route.clarifying_question.strip():
        return "clarify"
    ctx = runtime.context
    wants_help = route.needs_search or ctx.rag.hybrid(state["turns"], route.query)
    wants_account = route.needs_account and ctx.account is not None
    wants_past = route.needs_past and ctx.past is not None
    return "coordinator" if (wants_help or wants_account or wants_past) else "agent"


async def clarify(state: AgentState) -> dict:
    return {"answer": state["route"].clarifying_question.strip(), "clarified": True}


async def coordinator(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx, route, turns = runtime.context, state["route"], state["turns"]
    result = await ctx.rag.gather(
        route.query.strip() or turns[-1].content,
        search_help=route.needs_search or ctx.rag.hybrid(turns, route.query),
        account=ctx.account if route.needs_account else None,
        past=ctx.past if route.needs_past else None,
    )
    if result.grounding is None:
        return {"usage": result.usage}
    current = dataclasses.replace(turns[-1], context=result.grounding.context)
    return {"turns": [*turns[:-1], current], "usage": result.usage,
            "citations": [c.as_dict() for c in result.grounding.citations]}


async def agent(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    system = ctx.system + TOOLS_NOTE + ("" if state.get("route") else ROUTE_FAILED_NOTE)
    messages = to_messages(system, state["turns"]) + state.get("scratch", [])
    can_use_tools = state.get("tool_rounds", 0) < ctx.max_tool_rounds
    model = ctx.model.bind_tools([t.as_openai() for t in ctx.tools]) if can_use_tools \
        else ctx.model
    reply = await model.ainvoke(messages)
    used = usage_from_message(reply)
    if reply.invalid_tool_calls and not reply.tool_calls:
        log.warning("malformed tool call; retrying without tools",
                    extra={"errors": [c.get("error") for c in reply.invalid_tool_calls]})
        reply = await ctx.model.ainvoke(messages)
        used += usage_from_message(reply)
    if reply.tool_calls and can_use_tools:
        return {"scratch": [reply], "usage": used}
    return {"answer": reply.text, "usage": used}


async def tools(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    by_name = {t.name: t for t in ctx.tools}
    calls = state["scratch"][-1].tool_calls
    # The model may ask for several lookups at once; they're independent, so run together.
    outputs = await asyncio.gather(*(_run_tool(by_name, c) for c in calls))
    next_n = len(state.get("citations", [])) + 1
    citations: list[dict] = []
    results: list[BaseMessage] = []
    for call, output in zip(calls, outputs, strict=True):
        numbered = [{**c, "n": next_n + i} for i, c in enumerate(output.citations)]
        next_n += len(numbered)
        citations += numbered
        results.append(ToolMessage(output.render(numbered), tool_call_id=call["id"],
                                   status="error" if output.error else "success"))
    return {"scratch": results, "citations": citations,
            "tool_rounds": state.get("tool_rounds", 0) + 1}


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
