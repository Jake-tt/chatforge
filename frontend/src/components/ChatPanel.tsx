import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ApiError, Citation } from "../api";
import { markCited, shownSources } from "../sources";

interface Bubble {
  id: string;
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
  note?: string;
  citations?: Citation[] | null;
  suggestions?: string[];
}

interface Props {
  conversationId: string | null;
  isGuest: boolean;
  ensureConversation: () => Promise<string>;
  onReplyFinished: () => void;
  onLoginRequested: () => void;
}

const ERROR_TEXT: Record<string, string> = {
  quota_exceeded: "오늘 사용할 수 있는 토큰을 모두 썼어요. 내일 다시 시도해 주세요.",
  rate_limited: "메시지를 너무 빠르게 보내고 있어요. 잠시 후 다시 시도해 주세요.",
  llm_unavailable: "모델 응답이 중간에 끊겼어요. 다시 시도해 주세요.",
  stream_in_progress: "이 대화에서 아직 답변을 작성하고 있어요. 끝난 뒤 다시 보내 주세요.",
  too_many_streams: "동시에 진행 중인 답변이 너무 많아요. 잠시 후 다시 보내 주세요.",
};

export default function ChatPanel(props: Props) {
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // History is loaded once per mount; the parent remounts on navigation (see App panelKey).
  const initialId = useRef(props.conversationId);

  useEffect(() => {
    const id = initialId.current;
    if (!id) return;
    let cancelled = false;
    api.getConversation(id).then((conv) => {
      if (!cancelled) setBubbles(conv.messages.map((m) => ({ ...m })));
    });
    return () => {
      cancelled = true;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [bubbles]);

  const updateLast = (fn: (b: Bubble) => Bubble) =>
    setBubbles((prev) => [...prev.slice(0, -1), fn(prev[prev.length - 1])]);

  const send = async (text: string) => {
    const content = text.trim();
    if (!content || streaming) return;

    setInput("");
    setError(null);
    setStreaming(true);
    setBubbles((prev) => [
      ...prev,
      { id: `u-${Date.now()}`, role: "user", content },
      { id: `a-${Date.now()}`, role: "assistant", content: "", pending: true },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    let received = false; // any answer text arrived (state updates may not have run yet)
    try {
      const conversationId = await props.ensureConversation();
      const done = await api.sendMessage(
        conversationId,
        content,
        {
          onDelta: (delta) => {
            if (delta) received = true;
            updateLast((b) => ({ ...b, content: b.content + delta }));
          },
          onSources: (citations) => updateLast((b) => ({ ...b, citations })),
          onSuggestions: (suggestions) => updateLast((b) => ({ ...b, suggestions })),
        },
        controller.signal,
      );
      updateLast((b) => ({
        ...b,
        pending: false,
        note: done.stop_reason === "refusal" ? "이 요청에는 답변할 수 없어요." : undefined,
        citations: b.citations ? markCited(b.citations, done.cited) : b.citations,
      }));
    } catch (err) {
      if (received) {
        // Keep what streamed: the server stores (and bills) it as the reply.
        const note = controller.signal.aborted ? "응답을 중지했어요." : undefined;
        updateLast((b) => ({ ...b, pending: false, note }));
      } else {
        // Nothing arrived, so the server undid the whole turn (the user message too):
        // do the same here and put the question back to resend.
        setBubbles((prev) => prev.slice(0, -2));
        // Keep anything typed meanwhile; otherwise offer the question again.
        setInput((current) => (current.trim() ? current : content));
      }
      if (controller.signal.aborted) return;
      const apiErr = err instanceof ApiError ? err : null;
      setError({
        code: apiErr?.code ?? "unknown",
        message: (apiErr && ERROR_TEXT[apiErr.code]) ?? apiErr?.message ?? "문제가 발생했어요.",
      });
    } finally {
      setStreaming(false);
      abortRef.current = null;
      props.onReplyFinished();
    }
  };

  return (
    <section className="chat">
      <div className="messages">
        {bubbles.length === 0 && (
          <div className="empty">
            <h1>무엇이든 물어보세요</h1>
            {props.isGuest && (
              <p className="muted">
                로그인 없이 바로 쓸 수 있어요. 나중에 가입하면 지금까지의 대화가 계정으로 옮겨져요.
              </p>
            )}
          </div>
        )}
        {bubbles.map((b, i) => (
          <div key={b.id} className={`bubble ${b.role}`}>
            {b.content || (b.pending ? <span className="typing">●●●</span> : null)}
            {b.note && <div className="note">{b.note}</div>}
            {b.citations && b.citations.length > 0 && (
              <ol className="sources" aria-label="참고한 자료">
                {shownSources(b.citations).map((c) => (
                  <li key={c.n} value={c.n} title={c.snippet}>
                    {c.title}
                  </li>
                ))}
              </ol>
            )}
            {/* Only the latest answer offers follow-ups; older ones would be stale. */}
            {i === bubbles.length - 1 && !streaming && b.suggestions && b.suggestions.length > 0 && (
              <div className="suggestions">
                {b.suggestions.map((q) => (
                  <button key={q} type="button" className="chip" onClick={() => void send(q)}>
                    {q}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {error && (
        <div className="banner banner-error">
          {error.message}
          {error.code === "quota_exceeded" && props.isGuest && (
            <button className="btn btn-link" onClick={props.onLoginRequested}>
              가입하고 한도 늘리기
            </button>
          )}
        </div>
      )}

      <form
        className="composer"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void send(input);
        }}
      >
        <textarea
          value={input}
          placeholder="메시지를 입력하세요 (Shift+Enter 줄바꿈)"
          rows={1}
          maxLength={8000}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        {streaming ? (
          <button type="button" className="btn" onClick={() => abortRef.current?.abort()}>
            중지
          </button>
        ) : (
          <button type="submit" className="btn btn-primary" disabled={!input.trim()}>
            보내기
          </button>
        )}
      </form>
    </section>
  );
}
