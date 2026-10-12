"""Structured logging for Cloud Run.

Cloud Logging parses one JSON object per line on stdout: `severity` sets the level and
`logging.googleapis.com/trace` links the line to its Cloud Trace span, so every log line
written while handling a request can be found from that request's trace.

Each request gets a `request_id` (taken from X-Request-ID or generated) and, behind the
Google front end, a trace id from X-Cloud-Trace-Context. Both live in contextvars, so they
are attached to log lines from any code that runs for the request, including the SSE
generator and the in-process background tasks it starts.
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_var: ContextVar[str | None] = ContextVar("trace", default=None)

access_log = logging.getLogger("chatforge.access")

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class CloudRunJsonFormatter(logging.Formatter):
    def __init__(self, gcp_project: str = ""):
        super().__init__()
        self._project = gcp_project

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
        }
        if request_id := request_id_var.get():
            entry["request_id"] = request_id
        if (trace := trace_var.get()) and self._project:
            entry["logging.googleapis.com/trace"] = f"projects/{self._project}/traces/{trace}"
        # Anything passed via `extra=` becomes a structured field.
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(log_format: str, gcp_project: str = "") -> None:
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(CloudRunJsonFormatter(gcp_project))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        ))
    handler._chatforge = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    # Replace only our own handler, so re-creating the app (tests) doesn't stack handlers
    # or remove ones installed by others (e.g. pytest's log capture).
    root.handlers[:] = [h for h in root.handlers if not getattr(h, "_chatforge", False)]
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Uvicorn's own access log would duplicate RequestContextMiddleware's.
    logging.getLogger("uvicorn.access").disabled = True


class RequestContextMiddleware:
    """Pure ASGI middleware (doesn't buffer bodies, so SSE keeps streaming)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = headers.get("x-request-id") or uuid.uuid4().hex
        # Format: TRACE_ID/SPAN_ID;o=OPTIONS
        trace = headers.get("x-cloud-trace-context", "").split("/", 1)[0] or None
        rid_token, trace_token = request_id_var.set(request_id), trace_var.set(trace)

        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            access_log.info(
                "%s %s %s", scope["method"], scope["path"], status,
                extra={
                    "http_method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(rid_token)
            trace_var.reset(trace_token)
