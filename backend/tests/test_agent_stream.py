"""AgentStream: folds LangGraph `messages` and `updates` into SSE events. Pure logic."""

from langchain_core.messages import AIMessageChunk

from app.agent.stream import FALLBACK_ANSWER, HOLDBACK_CHARS, AgentStream
from app.services.llm import ChatTurn, Usage

AGENT = {"langgraph_node": "agent"}


def _chunk(text: str = "", run: str = "r1", tool: bool = False) -> AIMessageChunk:
    calls = [{"name": "search_help", "args": "{}", "id": "c", "index": 0}] if tool else []
    return AIMessageChunk(content=text, id=run, tool_call_chunks=calls)


def _feed(stream: AgentStream, *chunks: AIMessageChunk) -> list[tuple[str, dict]]:
    events = []
    for c in chunks:
        events += stream.on_message(c, AGENT)
    return events


def test_a_preamble_before_a_tool_call_is_never_shown():
    stream = AgentStream(turns=[ChatTurn("user", "q")])

    events = _feed(stream, _chunk("찾아볼게요"), _chunk(tool=True),
                   _chunk("해운대", run="r2"), _chunk(" 근처예요", run="r2"))
    events += stream.on_update({"answer": "해운대 근처예요", "usage": Usage()})

    assert [d["text"] for e, d in events if e == "delta"] == ["해운대", " 근처예요"]
    assert stream.text == "해운대 근처예요"


def test_a_long_answer_starts_streaming_once_past_the_holdback():
    stream = AgentStream(turns=[ChatTurn("user", "q")])
    long = "가" * (HOLDBACK_CHARS + 5)

    events = _feed(stream, _chunk(long[:HOLDBACK_CHARS - 1]), _chunk(long[HOLDBACK_CHARS - 1:]),
                   _chunk(" 끝"))

    # Held-back chunks go out as they arrived, then the rest streams live.
    assert [d["text"] for _, d in events] == [long[:HOLDBACK_CHARS - 1],
                                             long[HOLDBACK_CHARS - 1:], " 끝"]


def test_other_nodes_and_non_ai_messages_are_ignored():
    stream = AgentStream(turns=[ChatTurn("user", "q")])

    assert stream.on_message(_chunk("라우터 출력"), {"langgraph_node": "router"}) == []
    assert stream.on_message(object(), AGENT) == []


def test_updates_accumulate_usage_and_cumulative_sources():
    stream = AgentStream(turns=[ChatTurn("user", "q")])
    first = {"n": 1, "title": "a", "url": "/a", "snippet": "x"}
    second = {"n": 2, "title": "b", "url": "/b", "snippet": "y"}

    events = stream.on_update({"usage": Usage(10, 2), "citations": [first]})
    events += stream.on_update({"usage": Usage(5, 1), "citations": [second]})

    assert stream.usage == Usage(15, 3)
    assert [d["citations"] for e, d in events if e == "sources"] == [[first], [first, second]]


def test_an_answer_that_never_streamed_is_sent_whole():
    # The clarify node replies without a model call.
    stream = AgentStream(turns=[ChatTurn("user", "q")])

    events = stream.on_update({"answer": "어떤 걸 말씀하시나요?", "clarified": True})

    assert events == [("delta", {"text": "어떤 걸 말씀하시나요?"})]
    assert stream.clarified


def test_an_empty_final_answer_is_replaced_with_an_apology():
    stream = AgentStream(turns=[ChatTurn("user", "q")])

    events = stream.on_update({"answer": ""})

    assert events == [("delta", {"text": FALLBACK_ANSWER})]
    assert stream.text == FALLBACK_ANSWER


def test_partial_text_is_what_the_client_saw():
    stream = AgentStream(turns=[ChatTurn("user", "q")])

    _feed(stream, _chunk("가" * HOLDBACK_CHARS), _chunk("나"))

    assert stream.partial == "가" * HOLDBACK_CHARS + "나"
