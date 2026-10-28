"""A scripted LangChain chat model for agent tests: the LLM is the external boundary."""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

Reply = AIMessage | Callable[[list[BaseMessage], list[dict]], AIMessage] | BaseException


class ScriptedChatModel(BaseChatModel):
    """Answers each call with the next scripted reply and records what it was sent.

    A reply is an AIMessage, a function of (messages, bound tools) returning one, or an
    exception to raise. Tools bound with `bind_tools` are recorded per call, so tests can
    check that a retry ran without tools.
    """

    script: list[Any] = Field(default_factory=list)
    calls: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _next(self, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.script:
            raise AssertionError("ScriptedChatModel ran out of replies")
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(messages, tools) if callable(reply) else reply

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self._next(messages, kwargs.get("tools") or [])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None,
                       **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        message = self._next(messages, kwargs.get("tools") or [])
        words = message.text.split(" ") if message.text else []
        for i, word in enumerate(words):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=word if i == 0 else " " + word))
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(message.tool_calls)
            ],
            usage_metadata=message.usage_metadata,
            response_metadata=message.response_metadata,
        ))


def usage(input_tokens: int, output_tokens: int) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


def tool_call(name: str, call_id: str = "call-1", **args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}],
                     usage_metadata=usage(10, 5))


def structured(**fields) -> Callable[[list[BaseMessage], list[dict]], AIMessage]:
    """A structured-output reply: a tool call to the schema tool LangChain bound."""
    def reply(messages, tools):
        return AIMessage("", tool_calls=[{"name": tools[0]["function"]["name"],
                                          "args": fields, "id": "structured-1"}],
                         usage_metadata=usage(20, 4))
    return reply
