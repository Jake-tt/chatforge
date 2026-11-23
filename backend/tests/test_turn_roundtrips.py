"""How many times a turn waits on Postgres or Redis before its reply starts streaming.

Every wait here comes before the first token, so each one is added to what the user
waits for. The budgets are what a turn needs today; a change that adds a round trip to
this path should fail here and say why it is worth it.
"""

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from sqlalchemy import event

from app.routers import conversations
from tests.fakes import RecordingChatModel


@pytest.fixture
def trips(monkeypatch):
    """Round trips logged in order: each SQL statement and commit, each Redis command or
    pipeline. `marks` holds how many there were when the reply started streaming."""
    log: list[str] = []
    marks: list[int] = []
    command, execute = Redis.execute_command, Pipeline.execute

    async def counted_command(self, *args, **kwargs):
        if not isinstance(self, Pipeline):  # a pipeline's commands go in one trip
            log.append(f"redis {args[0]}")
        return await command(self, *args, **kwargs)

    async def counted_pipeline(self, *args, **kwargs):
        log.append("redis " + ",".join(str(c[0][0]) for c in self.command_stack))
        return await execute(self, *args, **kwargs)

    monkeypatch.setattr(Redis, "execute_command", counted_command)
    monkeypatch.setattr(Pipeline, "execute", counted_pipeline)
    start = conversations.TurnResponse.__init__

    def streaming(self, *args, **kwargs):
        marks.append(len(log))
        start(self, *args, **kwargs)

    monkeypatch.setattr(conversations.TurnResponse, "__init__", streaming)

    def watch(engine) -> None:
        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def statement(conn, cursor, sql, *args):
            log.append("sql " + " ".join(sql.split())[:80])

        @event.listens_for(engine.sync_engine, "commit")
        def commit(conn):
            log.append("sql COMMIT")

    return log, marks, watch


@pytest.mark.parametrize(("who", "budget"), [("guest", 14), ("member", 15)])
async def test_a_follow_up_turn_starts_streaming_within_its_round_trip_budget(
        make_harness, trips, who, budget):
    log, marks, watch = trips
    h = await make_harness(chat_model=RecordingChatModel())
    headers = await (h.guest() if who == "guest" else h.user())  # members remember
    cid = await h.new_conversation(headers)
    await h.chat(headers, cid, "첫 질문")
    watch(h.ctx.engine)
    log.clear()
    marks.clear()

    result = await h.chat(headers, cid, "두 번째 질문")

    assert result.last("done")
    before_stream = log[:marks[0]]
    assert len(before_stream) <= budget, "\n".join(before_stream)
