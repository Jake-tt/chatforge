import json
import logging

from app.observability import CloudRunJsonFormatter, request_id_var, trace_var


async def test_request_id_is_echoed_or_generated(h):
    r = await h.client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"

    r = await h.client.get("/healthz")
    assert len(r.headers["x-request-id"]) == 32


async def test_access_log_carries_request_context(h, caplog):
    caplog.set_level(logging.INFO, logger="chatforge.access")
    await h.client.get(
        "/healthz",
        headers={"X-Request-ID": "req-1", "X-Cloud-Trace-Context": "105445aa7843bc8bf206b1/1;o=1"},
    )
    record = next(r for r in caplog.records if r.name == "chatforge.access")
    assert (record.http_method, record.path, record.status) == ("GET", "/healthz", 200)

    line = json.loads(CloudRunJsonFormatter("my-proj").format(record))
    assert line["severity"] == "INFO"
    assert line["status"] == 200
    assert "latency_ms" in line


def test_json_formatter_links_trace_and_request():
    rid, trace = request_id_var.set("req-9"), trace_var.set("abc")
    try:
        record = logging.makeLogRecord(
            {"name": "x", "levelname": "WARNING", "msg": "redis down: %s", "args": ("timeout",),
             "conversation_id": "c1"}
        )
        line = json.loads(CloudRunJsonFormatter("my-proj").format(record))
    finally:
        request_id_var.reset(rid)
        trace_var.reset(trace)

    assert line["message"] == "redis down: timeout"
    assert line["severity"] == "WARNING"
    assert line["request_id"] == "req-9"
    assert line["logging.googleapis.com/trace"] == "projects/my-proj/traces/abc"
    assert line["conversation_id"] == "c1"
