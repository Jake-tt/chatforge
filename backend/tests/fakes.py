"""A scripted LangChain chat model for agent tests: the LLM is the external boundary."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from app.services.llm import FakeProvider, LLMError

Reply = AIMessage | Callable[[list[BaseMessage], list[dict]], AIMessage] | BaseException


class ScriptedChatModel(BaseChatModel):
    """Answers each call with the next scripted reply and records what it was sent.

    A reply is an AIMessage, a function of (messages, bound tools) returning one, or an
    exception to raise. Tools bound with `bind_tools` are recorded per call, so tests can
    check that a retry ran without tools.
    """

    script: list[Any] = Field(default_factory=list)
    calls: list[dict] = Field(default_factory=list)
    delay: float = 0.0  # seconds before each streamed reply (timeouts)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def fallback(self, messages: list[BaseMessage], n: int) -> AIMessage:
        raise AssertionError("ScriptedChatModel ran out of replies")

    def _next(self, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.script:
            return self.fallback(messages, len(self.calls))
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(messages, tools) if callable(reply) else reply

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self._next(messages, kwargs.get("tools") or [])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None,
                       **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        if self.delay:
            await asyncio.sleep(self.delay)
        message = self._next(messages, kwargs.get("tools") or [])
        words = message.text.split(" ") if message.text else []
        for i, word in enumerate(words):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=word if i == 0 else " " + word))
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="",
            # Invalid calls go out with their raw, unparseable args, as a provider streams
            # them; LangChain turns them back into `invalid_tool_calls` when merging.
            tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(message.tool_calls)
            ] + [
                {"name": c["name"], "args": c["args"], "id": c["id"],
                 "index": len(message.tool_calls) + i}
                for i, c in enumerate(message.invalid_tool_calls)
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


def _text(message: BaseMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else "".join(b["text"] for b in content)


class RecordingChatModel(ScriptedChatModel):
    """Answers "reply N" for the Nth call unless scripted, and exposes what each call saw.

    It never calls tools, so on the agent graph it answers whatever it is asked.
    """

    def fallback(self, messages: list[BaseMessage], n: int) -> AIMessage:
        prompt = sum(len(_text(m).split()) for m in messages)
        return AIMessage(f"reply {n}", usage_metadata=usage(prompt, 2))

    @property
    def systems(self) -> list[str]:
        return [_text(c["messages"][0]) for c in self.calls
                if isinstance(c["messages"][0], SystemMessage)]

    def turns(self, call: int = -1) -> list[tuple[str, str]]:
        """(role, text) of one call's conversation, system prompt excluded."""
        return [("user" if isinstance(m, HumanMessage) else "assistant", _text(m))
                for m in self.calls[call]["messages"] if not isinstance(m, SystemMessage)]

    def contents(self, call: int = -1) -> list[str]:
        return [text for _, text in self.turns(call)]

    @property
    def current_turns(self) -> list[str]:
        """The current user turn of each call, with any sources attached in front."""
        return [self.turns(i)[-1][1] for i in range(len(self.calls))]


class NoRouterProvider(FakeProvider):
    """Routing fails without spending tokens, so a test can bill the answer alone."""

    async def structured(self, system, prompt, schema):
        raise LLMError("router disabled for this test", retryable=False)


def cached_answer(input_tokens: int, output_tokens: int, cache_read: int = 0,
                  cache_write: int = 0) -> AIMessage:
    """An answer whose usage_metadata reports prompt-cache tokens the way LangChain does:
    `input_tokens` is the whole prompt, cached parts included."""
    return AIMessage("cached answer", usage_metadata={
        "input_tokens": input_tokens + cache_read + cache_write,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + cache_read + cache_write + output_tokens,
        "input_token_details": {"cache_read": cache_read, "cache_creation": cache_write},
    })
