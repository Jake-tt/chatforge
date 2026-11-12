"""Automatic memory extraction: an hour after a member's last reply, facts they shared in
the conversation are pulled out and remembered.

The extractor is the auxiliary provider's structured call, scripted here; scheduling,
the cursor, filtering and storage are real and driven over HTTP and the task endpoint.
"""

import asyncio

from app.routers.internal import verify_task_caller
from app.services.llm import TextResult, Usage
from app.services.memory_extraction import EXTRACT_SYSTEM, TASK_NAME, ExtractedFacts
from tests.fakes import RecordingChatModel
from tests.test_internal_tasks import RecordingQueue

MEMBER = "extract@example.com"


def fact(subject: str, predicate: str, obj: str, is_name: bool = False) -> dict:
    return {"subject": subject, "predicate": predicate, "object": obj, "object_is_name": is_name,
            "category": "other"}


class ScriptedExtractor:
    """Answers the extraction call with scripted facts and records its prompts."""

    def __init__(self, facts: list[dict] | None = None, error: bool = False,
                 delay: float = 0.0):
        self.facts = facts or []
        self.error = error
        self.delay = delay
        self.prompts: list[str] = []

    async def structured(self, system, prompt, schema):
        if schema is not ExtractedFacts:
            return schema.fake(prompt), Usage()
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        if self.error:
            raise ConnectionError("extractor down")
        return ExtractedFacts.model_validate({"facts": self.facts}), Usage(300, 50)

    async def complete(self, system, prompt, images=()):
        return TextResult("", Usage())  # titles: not under test


async def _setup(make_harness, llm: ScriptedExtractor, *, guest: bool = False, **settings):
    queue = RecordingQueue()
    h = await make_harness(llm=llm, chat_model=RecordingChatModel(script=[]),
                           task_backend="cloudtasks", tasks=queue, **settings)
    h.app.dependency_overrides[verify_task_caller] = lambda: None
    if guest:
        headers = await h.guest()
    else:
        r = await h.signup(MEMBER)
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
    cid = await h.new_conversation(headers)
    return h, queue, headers, cid


def _scheduled(queue) -> list[tuple[dict, float]]:
    return [(payload, delay)
            for (name, payload), delay in zip(queue.enqueued, queue.delays, strict=True)
            if name == TASK_NAME]


async def _deliver(h, payload: dict) -> None:
    r = await h.client.post(f"/internal/tasks/{TASK_NAME}", json=payload)
    assert r.status_code == 204


async def _remembered(h, headers) -> list[tuple[str, str, str]]:
    body = (await h.client.get("/memory", headers=headers)).json()
    return sorted((f["subject"], f["predicate"], f["object"]) for f in body["facts"])


async def test_each_member_reply_schedules_extraction_an_hour_later(make_harness):
    h, queue, headers, cid = await _setup(make_harness, ScriptedExtractor())

    await h.chat(headers, cid, "저는 부산에 살아요")

    assert _scheduled(queue) == [({"conversation_id": cid, "after_seq": 2}, 3600)]


async def test_guests_and_members_with_memory_off_are_not_scheduled(make_harness):
    h, queue, headers, cid = await _setup(make_harness, ScriptedExtractor(), guest=True)
    await h.chat(headers, cid, "저는 부산에 살아요")
    assert _scheduled(queue) == []

    h, queue, headers, cid = await _setup(make_harness, ScriptedExtractor())
    await h.client.put("/memory/settings", headers=headers, json={"enabled": False})
    await h.chat(headers, cid, "저는 부산에 살아요")
    assert _scheduled(queue) == []


async def test_extraction_can_be_turned_off(make_harness):
    h, queue, headers, cid = await _setup(make_harness, ScriptedExtractor(),
                                          memory_extraction_seconds=0)

    await h.chat(headers, cid, "저는 부산에 살아요")

    assert _scheduled(queue) == []


async def test_an_idle_conversation_is_remembered_under_the_member(make_harness):
    llm = ScriptedExtractor([fact("사용자", "사는 곳", "부산", True),
                             fact("나", "좋아하는 음료", "라떼")])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살고 라떼를 좋아해요")

    await _deliver(h, _scheduled(queue)[-1][0])

    assert "저는 부산에 살고 라떼를 좋아해요" in llm.prompts[0]
    # Both ways of naming the member land on the one entity `remember` uses.
    assert await _remembered(h, headers) == [("나", "사는 곳", "부산"),
                                             ("나", "좋아하는 음료", "라떼")]


async def test_extraction_does_nothing_once_the_conversation_moved_on(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")
    await h.chat(headers, cid, "날씨 어때요?")

    await _deliver(h, _scheduled(queue)[0][0])  # scheduled after the first reply

    assert llm.prompts == []
    assert await _remembered(h, headers) == []


async def test_later_runs_only_read_messages_after_the_cursor(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")
    await _deliver(h, _scheduled(queue)[-1][0])

    await h.chat(headers, cid, "고양이를 키워요")
    await _deliver(h, _scheduled(queue)[-1][0])

    assert "고양이를 키워요" in llm.prompts[1]
    assert "부산" not in llm.prompts[1]


async def test_sensitive_categories_are_excluded_by_prompt_and_by_pattern(make_harness):
    llm = ScriptedExtractor([
        fact("나", "카드 번호", "4111 1111 1111 1111"),
        fact("나", "비밀번호", "비밀번호는 hunter2"),
        fact("나", "주민번호", "900101-1234567"),
        fact("나", "계좌", "110-123-456789"),
        fact("나", "사는 곳", "부산", True),
    ])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "카드랑 비밀번호 알려줄게요")

    await _deliver(h, _scheduled(queue)[-1][0])

    for category in ("health", "financial", "identification", "password", "location",
                     "political", "religious"):
        assert category in EXTRACT_SYSTEM.lower()
    assert await _remembered(h, headers) == [("나", "사는 곳", "부산")]


async def test_a_fact_already_remembered_only_gains_a_source(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")
    await _deliver(h, _scheduled(queue)[-1][0])
    other = await h.new_conversation(headers)
    await h.chat(headers, other, "부산 사는 사람인데요")

    await _deliver(h, _scheduled(queue)[-1][0])
    assert await _remembered(h, headers) == [("나", "사는 곳", "부산")]

    # Confirmed in two conversations: deleting one keeps it.
    await h.client.delete(f"/conversations/{cid}", headers=headers)
    assert await _remembered(h, headers) == [("나", "사는 곳", "부산")]


async def test_a_duplicate_delivery_extracts_once(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)], delay=0.05)
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")
    payload = _scheduled(queue)[-1][0]

    await asyncio.gather(_deliver(h, payload), _deliver(h, payload))
    await _deliver(h, payload)

    assert await _remembered(h, headers) == [("나", "사는 곳", "부산")]
    assert len(llm.prompts) <= 2  # a third, after the cursor moved, doesn't even ask


async def test_a_failed_extraction_stores_nothing_and_is_retried(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)], error=True)
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")

    await _deliver(h, _scheduled(queue)[-1][0])
    assert await _remembered(h, headers) == []

    llm.error = False
    await h.chat(headers, cid, "고양이를 키워요")
    await _deliver(h, _scheduled(queue)[-1][0])

    # The cursor stayed put, so the next run reads the earlier message again.
    assert "저는 부산에 살아요" in llm.prompts[-1]
    assert await _remembered(h, headers) == [("나", "사는 곳", "부산")]


async def test_a_timed_out_extraction_stores_nothing(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)], delay=0.5)
    h, queue, headers, cid = await _setup(make_harness, llm,
                                          memory_extraction_timeout_seconds=0.05)
    await h.chat(headers, cid, "저는 부산에 살아요")

    await _deliver(h, _scheduled(queue)[-1][0])

    assert await _remembered(h, headers) == []


async def test_extraction_is_not_charged_to_the_member(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")
    used_before = (await h.client.get("/usage", headers=headers)).json()["used"]

    await _deliver(h, _scheduled(queue)[-1][0])

    assert llm.prompts, "extraction should have run"
    assert (await h.client.get("/usage", headers=headers)).json()["used"] == used_before


async def test_turning_memory_off_before_the_run_stops_it(make_harness):
    llm = ScriptedExtractor([fact("나", "사는 곳", "부산", True)])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "저는 부산에 살아요")
    await h.client.put("/memory/settings", headers=headers, json={"enabled": False})

    await _deliver(h, _scheduled(queue)[-1][0])

    assert llm.prompts == []
    assert await _remembered(h, headers) == []


async def test_facts_the_model_files_under_a_sensitive_category_are_dropped(make_harness):
    # A local 8B model extracted health, politics and religion despite being told not to,
    # yet labelled them correctly: the label is checked in code.
    llm = ScriptedExtractor([
        {**fact("나", "복용 약", "우울증 약"), "category": "health"},
        {**fact("나", "지지 정당", "OO당"), "category": "politics"},
        {**fact("나", "종교", "기독교"), "category": "religion"},
        {**fact("나", "사는 곳", "부산 해운대구 우동"), "category": "precise_location"},
        {**fact("나", "연봉", "6200만원"), "category": "finance"},
        {**fact("나", "사는 도시", "부산", True), "category": "place"},
    ])
    h, queue, headers, cid = await _setup(make_harness, llm)
    await h.chat(headers, cid, "이것저것 말해볼게요")

    await _deliver(h, _scheduled(queue)[-1][0])

    assert await _remembered(h, headers) == [("나", "사는 도시", "부산")]


def test_extracted_facts_are_cleaned_and_capped():
    raw = [fact(" 나 ", "사는<ctrl46> 곳", "부산", True), fact("", "x", "y"),
           fact("나", "", "y"), fact("나", "x", "  ")] + \
          [fact("나", f"취미 {i}", f"값 {i}") for i in range(20)]

    facts = ExtractedFacts.model_validate({"facts": raw}).facts

    assert facts[0].subject == "나" and facts[0].predicate == "사는 곳"
    assert all(f.subject and f.predicate and f.object for f in facts)
    assert len(facts) == 10
