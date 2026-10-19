"""Rolling conversation summary: older turns are replaced by a summary in the context."""

import asyncio
from collections.abc import AsyncIterator

from app.routers.internal import verify_task_caller
from app.services.llm import ChatTurn, Completion, StreamEvent, TextDelta, TextResult, Usage
from tests.test_internal_tasks import RecordingQueue


class ScriptedProvider:
    """Replies from a script, records what each call was sent, and fakes the summarizer."""

    def __init__(self, replies: list[str] | None = None, summary: str = "SUMMARY",
                 summary_delay: float = 0.0, summary_error: bool = False):
        self._replies = list(replies or [])
        self._summary = summary
        self._summary_delay = summary_delay
        self._summary_error = summary_error
        self.systems: list[str] = []
        self.calls: list[list[ChatTurn]] = []
        self.summary_prompts: list[str] = []

    async def stream(self, system: str, turns: list[ChatTurn]) -> AsyncIterator[StreamEvent]:
        self.systems.append(system)
        self.calls.append(turns)
        n = len(self.calls)
        yield TextDelta(self._replies[n - 1] if n <= len(self._replies) else f"reply {n}")
        yield Completion(input_tokens=1, output_tokens=1, stop_reason="end_turn")

    async def complete(self, system: str, prompt: str) -> TextResult:
        if "summary" not in system.lower():
            return TextResult("", Usage())  # conversation title: not under test
        self.summary_prompts.append(prompt)
        await asyncio.sleep(self._summary_delay)
        if self._summary_error:
            raise ConnectionError("summarizer down")
        return TextResult(self._summary, Usage(100, 20))


SUMMARY_ON = {"summary_enabled": True, "summary_trigger_messages": 6, "summary_keep_turns": 1}


async def _chat_turns(h, headers, cid, *messages: str) -> None:
    for message in messages:
        assert (await h.chat(headers, cid, message)).response.status_code == 200
        await h.ctx.tasks.drain()


async def test_older_turns_are_replaced_by_the_summary(make_harness):
    llm = ScriptedProvider(summary="사용자는 부산 여행을 계획 중이다.")
    h = await make_harness(llm=llm, **SUMMARY_ON)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    # The third reply makes 6 unsummarized messages: summarize all but the last turn.
    await _chat_turns(h, headers, cid, "one", "two", "three")
    await _chat_turns(h, headers, cid, "four")

    assert "one" in llm.summary_prompts[0] and "reply 2" in llm.summary_prompts[0]
    assert "three" not in llm.summary_prompts[0]
    assert "사용자는 부산 여행을 계획 중이다." in llm.systems[-1]
    assert [(t.role, t.content) for t in llm.calls[-1]] == [
        ("user", "three"), ("assistant", "reply 3"), ("user", "four"),
    ]


async def test_a_pending_clarifying_question_is_never_summarized_away(make_harness):
    # Production bug this guards against: summarizing while the assistant was asking the
    # user what they meant dropped the original question, so the user's answer ("프로")
    # reached the model with nothing left to answer.
    llm = ScriptedProvider(replies=["안녕하세요", "네", "어느 요금제로 바꾸시겠어요?"])
    h = await make_harness(llm=llm, **SUMMARY_ON)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    await _chat_turns(h, headers, cid, "안녕", "질문이 있어", "요금제를 바꾸고 싶어")
    await _chat_turns(h, headers, cid, "프로")

    assert llm.summary_prompts, "summary should have run"
    assert "어느 요금제로" not in llm.summary_prompts[0]
    assert [t.content for t in llm.calls[-1]] == [
        "요금제를 바꾸고 싶어", "어느 요금제로 바꾸시겠어요?", "프로",
    ]


async def test_summarizer_failure_keeps_the_full_history(make_harness):
    llm = ScriptedProvider(summary_error=True)
    h = await make_harness(llm=llm, **SUMMARY_ON)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    await _chat_turns(h, headers, cid, "one", "two", "three", "four")

    assert llm.systems[-1] == h.ctx.settings.system_prompt
    assert [t.content for t in llm.calls[-1]][:2] == ["one", "reply 1"]


async def test_an_overlong_summary_is_discarded(make_harness):
    llm = ScriptedProvider(summary="x" * 500)
    h = await make_harness(llm=llm, conversation_summary_char_limit=100, **SUMMARY_ON)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    await _chat_turns(h, headers, cid, "one", "two", "three", "four")

    assert llm.summary_prompts and "xxx" not in llm.systems[-1]
    assert llm.calls[-1][0].content == "one"


async def test_summary_tokens_are_charged_once_despite_duplicate_delivery(make_harness):
    queue = RecordingQueue()
    llm = ScriptedProvider(summary_delay=0.05)
    h = await make_harness(llm=llm, task_backend="cloudtasks", tasks=queue, **SUMMARY_ON)
    h.app.dependency_overrides[verify_task_caller] = lambda: None
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    for message in ("one", "two", "three"):
        await h.chat(headers, cid, message)
    used_before = (await h.client.get("/usage", headers=headers)).json()["used"]

    [payload] = [p for name, p in queue.enqueued if name == "summarize_conversation"]
    # Cloud Tasks delivers at least once: two overlapping deliveries of the same task.
    responses = await asyncio.gather(*(
        h.client.post("/internal/tasks/summarize_conversation", json=payload) for _ in range(2)
    ))

    assert [r.status_code for r in responses] == [204, 204]
    assert len(llm.summary_prompts) == 2
    used = (await h.client.get("/usage", headers=headers)).json()["used"]
    assert used - used_before == 100 + 20 * 5  # one summary's tokens, output weighted


async def test_short_conversations_are_not_summarized(make_harness):
    queue = RecordingQueue()
    h = await make_harness(llm=ScriptedProvider(), task_backend="cloudtasks", tasks=queue,
                           **SUMMARY_ON)
    headers = await h.guest()
    cid = await h.new_conversation(headers)

    await h.chat(headers, cid, "one")
    await h.chat(headers, cid, "two")

    assert "summarize_conversation" not in [name for name, _ in queue.enqueued]
