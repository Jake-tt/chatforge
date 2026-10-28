"""Turns the agent graph's stream into the chat SSE events.

LangGraph's `messages` mode yields every model token, but an agent round that ends in a
tool call may first write a preamble ("let me look that up"). Streaming it would glue it
onto the final answer. So each model call's text is held back until it is either clearly
an answer (past `HOLDBACK_CHARS` with no tool call) or the round ends; a round that turns
into a tool call drops what it held. The stored reply is the graph's final `answer`, not
the concatenation of every round.

`updates` mode yields each node's state delta: usage to bill, citations (sent as a
cumulative `sources` event), the grounded turns, the answer, the stop reason.
"""

from dataclasses import dataclass, field

from langchain_core.messages import AIMessageChunk

from app.services.llm import ChatTurn, Usage

HOLDBACK_CHARS = 80
FALLBACK_ANSWER = "죄송해요, 지금은 답을 정리하지 못했어요. 질문을 조금 바꿔 다시 물어봐 주세요."

Event = tuple[str, dict]


@dataclass
class AgentStream:
    turns: list[ChatTurn]
    usage: Usage = field(default_factory=Usage)
    citations: list[dict] = field(default_factory=list)
    answer: str | None = None
    stop_reason: str = "end_turn"
    clarified: bool = False
    streamed: list[str] = field(default_factory=list)  # every delta sent to the client
    _round: str | None = None  # id of the model call being streamed
    _held: list[str] = field(default_factory=list)
    _live: bool = False  # this round passed the holdback and streams directly
    _tool_round: bool = False
    _round_sent: bool = False

    @property
    def text(self) -> str:
        return self.answer if self.answer is not None else ""

    @property
    def partial(self) -> str:
        return "".join(self.streamed)

    def on_message(self, chunk: object, meta: dict) -> list[Event]:
        if meta.get("langgraph_node") != "agent" or not isinstance(chunk, AIMessageChunk):
            return []
        if chunk.id != self._round:
            self._round, self._held, self._live = chunk.id, [], False
            self._tool_round = self._round_sent = False
        if chunk.tool_call_chunks:
            self._tool_round, self._held = True, []
            return []
        if not chunk.text or self._tool_round:
            return []
        if self._live:
            return self._send([chunk.text])
        self._held.append(chunk.text)
        if sum(map(len, self._held)) >= HOLDBACK_CHARS:
            self._live = True
            return self._flush()
        return []

    def on_update(self, update: dict) -> list[Event]:
        events: list[Event] = []
        self.usage += update.get("usage") or Usage()
        self.turns = update.get("turns", self.turns)
        self.stop_reason = update.get("stop_reason", self.stop_reason)
        self.clarified = self.clarified or update.get("clarified", False)
        if update.get("citations"):
            self.citations = [*self.citations, *update["citations"]]
            events.append(("sources", {"citations": self.citations}))
        if "answer" in update:
            events += self._settle(update["answer"])
        return events

    def _settle(self, answer: str) -> list[Event]:
        """The final round ended as an answer: release what it held, or send it whole
        when nothing streamed (the clarify node, a model that doesn't stream)."""
        events = self._flush() if not self._tool_round and self._held else []
        self.answer = answer.strip() or FALLBACK_ANSWER
        if not self._round_sent:
            events += self._send([self.answer])
        return events

    def _flush(self) -> list[Event]:
        held, self._held = self._held, []
        return self._send(held)

    def _send(self, texts: list[str]) -> list[Event]:
        self._round_sent = True
        self.streamed += texts
        return [("delta", {"text": t}) for t in texts]
