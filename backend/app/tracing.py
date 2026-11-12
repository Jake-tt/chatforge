"""LLM tracing with Langfuse, for development and evaluation only.

Off by default (`LANGFUSE_ENABLED`), and refused on Cloud Run by the settings check: a
production trace would carry members' conversations and memories. When on, the Langfuse
LangChain handler is attached to the answer graph's run (router, agent, tools and the
calls inside them) and to the auxiliary calls made outside it (suggestions, titles,
summaries, memory extraction), each tagged with the request id; a turn's trace also
carries its conversation as the Langfuse session.

The handler is a LangChain callback: LangChain logs and swallows a callback's errors, so a
tracing backend that is down or slow never fails a reply.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.observability import request_id_var

if TYPE_CHECKING:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.runnables import RunnableConfig

    from app.config import Settings


class Tracer:
    def __init__(self, handler: BaseCallbackHandler | None = None,
                 flush: Callable[[], None] | None = None):
        self._handler = handler
        self._flush = flush

    @property
    def enabled(self) -> bool:
        return self._handler is not None

    def config(self, *, session_id: str | None = None,
               run_name: str | None = None) -> RunnableConfig:
        """The LangChain run config that traces a call ({} when tracing is off)."""
        if self._handler is None:
            return {}
        metadata: dict[str, Any] = {"request_id": request_id_var.get()}
        if session_id is not None:
            metadata["langfuse_session_id"] = session_id
        config: RunnableConfig = {"callbacks": [self._handler], "metadata": metadata}
        if run_name is not None:
            config["run_name"] = run_name
        return config

    def flush(self) -> None:
        """Send what is still queued (at shutdown). Never raises on network errors."""
        if self._flush is not None:
            self._flush()


def build_tracer(settings: Settings, handler: BaseCallbackHandler | None = None) -> Tracer:
    """`handler` stands in for Langfuse's in tests; otherwise it is created from the
    standard LANGFUSE_* environment (keys and host)."""
    if not settings.langfuse_enabled:
        return Tracer()
    if handler is not None:
        return Tracer(handler)
    # Optional dependency: pip install .[observability]
    from langfuse import get_client
    from langfuse.langchain import CallbackHandler
    return Tracer(CallbackHandler(), flush=get_client().flush)
