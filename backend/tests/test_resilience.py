"""Behaviour when Redis is unavailable."""

from redis.exceptions import ConnectionError as RedisConnectionError


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


async def test_circuit_breaker_stops_calling_redis_after_a_failure(make_harness):
    redis = DownRedis()
    h = await make_harness(redis=redis)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    for _ in range(3):
        await h.chat(headers, cid, "hi")
        await h.client.get("/usage", headers=headers)

    # Only the very first call reached Redis; the rest were short-circuited to the DB.
    assert redis.calls == 1
