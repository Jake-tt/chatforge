import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";

interface Bubble {
  id: string;
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
  note?: string;
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

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const content = input.trim();
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
    try {
      const conversationId = await props.ensureConversation();
      const done = await api.sendMessage(
        conversationId,
        content,
        (text) => updateLast((b) => ({ ...b, content: b.content + text })),
        controller.signal,
      );
      updateLast((b) => ({
        ...b,
        pending: false,
        note: done.stop_reason === "refusal" ? "이 요청에는 답변할 수 없어요." : undefined,
      }));
    } catch (err) {
      if (controller.signal.aborted) {
        // Stopped by the user; the server keeps (and bills) what was generated so far.
        updateLast((b) => ({ ...b, pending: false, note: "응답을 중지했어요." }));
        return;
      }
      const apiErr = err instanceof ApiError ? err : null;
      // Keep whatever streamed before the failure; drop the bubble if nothing arrived.
      setBubbles((prev) => {
        const last = prev[prev.length - 1];
        return last.content ? [...prev.slice(0, -1), { ...last, pending: false }] : prev.slice(0, -1);
      });
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
        {bubbles.map((b) => (
          <div key={b.id} className={`bubble ${b.role}`}>
            {b.content || (b.pending ? <span className="typing">●●●</span> : null)}
            {b.note && <div className="note">{b.note}</div>}
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

      <form className="composer" onSubmit={send}>
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
