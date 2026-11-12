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

import re
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
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    # Built from Settings, not the SDK's own environment lookup: `.env` values never reach
    # os.environ, and a missing host would fall back to Langfuse Cloud.
    public_key = settings.langfuse_public_key.get_secret_value()
    client = Langfuse(public_key=public_key,
                      secret_key=settings.langfuse_secret_key.get_secret_value(),
                      base_url=settings.langfuse_host, mask=mask_images)
    return Tracer(CallbackHandler(public_key=public_key), flush=client.flush)


_DATA_URL = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")
IMAGE_PLACEHOLDER = "<image omitted>"


def mask_images(*, data: Any, **kwargs: Any) -> Any:
    """Langfuse `mask`: images left out of traces (base64 blocks and data URLs), so a
    local trace store never keeps the pictures members attached."""
    if isinstance(data, dict):
        return {k: IMAGE_PLACEHOLDER if k == "base64" else mask_images(data=v)
                for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask_images(data=v) for v in data]
    if isinstance(data, str) and "base64," in data:
        return _DATA_URL.sub(IMAGE_PLACEHOLDER, data)
    return data
