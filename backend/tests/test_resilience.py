"""Behaviour when Redis is unavailable."""

import asyncio
import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.main import build_redis


class DownRedis:
    """Every call fails the way redis-py does when the server is unreachable."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls += 1
            raise RedisConnectionError("redis is down")
        return fail

    async def aclose(self):
        pass


async def test_chat_works_and_quota_still_enforced_without_redis(make_harness):
    h = await make_harness(redis=DownRedis(), guest_daily_tokens=10)
    headers = await h.guest()  # guest-creation rate limit fails open
    cid = await h.new_conversation(headers)

    assert (await h.chat(headers, cid, "use up the small daily quota now")).response.status_code \
        == 200
    usage = (await h.client.get("/usage", headers=headers)).json()
    assert usage["used"] >= 10  # read straight from the database

    blocked = await h.chat(headers, cid, "more")
    assert blocked.response.status_code == 429
    assert blocked.response.json()["detail"]["code"] == "quota_exceeded"


async def test_circuit_breaker_stops_calling_redis_after_repeated_failures(make_harness):
    redis = DownRedis()
    h = await make_harness(redis=redis, redis_failure_threshold=2)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    for _ in range(3):
        await h.chat(headers, cid, "hi")
        await h.client.get("/usage", headers=headers)

    # Only the first two calls reached Redis; the rest were short-circuited to the DB.
    assert redis.calls == 2


async def test_redis_is_probed_again_once_the_cooldown_ends(make_harness):
    redis = DownRedis()
    h = await make_harness(redis=redis, redis_failure_threshold=1, redis_cooldown_seconds=0)
    for _ in range(3):
        await h.client.post("/auth/guest")
    assert redis.calls == 3  # every call after the zero cooldown is a probe


@pytest.fixture
async def silent_redis_url():
    """A server that accepts connections and never answers, like a hung Redis."""
    writers = []

    async def hold(reader, writer):
        writers.append(writer)
        await reader.read()  # until the client gives up

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"redis://127.0.0.1:{port}/0"
    for w in writers:
        w.close()
    server.close()


async def test_a_hung_redis_costs_a_bounded_wait_not_a_stuck_request(make_harness,
                                                                     make_settings,
                                                                     silent_redis_url):
    # A Redis that accepts connections and never answers must cost about one socket
    # timeout, not the client's default of several seconds.
    # Only the read timeout is exercised: the server accepts the connection. Testing the
    # connect timeout needs an address that drops SYNs, which depends on the network.
    timeouts = {"redis_socket_timeout_seconds": 0.2, "redis_connect_timeout_seconds": 0.2}
    redis = build_redis(make_settings(redis_url=silent_redis_url, **timeouts))
    h = await make_harness(redis=redis, **timeouts)

    started = time.monotonic()
    async with asyncio.timeout(5):
        r = await h.client.post("/auth/guest")  # one rate-limit call, which fails open
    assert r.status_code == 201
    # One timeout, not one per client-side retry.
    assert time.monotonic() - started < 1
