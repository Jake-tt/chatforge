import { memo } from "react";
import type { AttachmentRef, Citation } from "../api";
import { chipsFor } from "../chips";
import { shownSources } from "../sources";
import Markdown from "./Markdown";

export interface Bubble {
  id: string;
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
  note?: string;
  citations?: Citation[] | null;
  suggestions?: string[];
  options?: string[] | null; // set on a clarifying question
  attachments?: AttachmentRef[];
}

type Pick = (text: string) => Promise<void>;

interface Props {
  bubble: Bubble;
  onPick?: Pick; // given only to the bubble that offers chips (see chipHandler)
}

// Memoized: while a reply streams, only its own bubble changes, so the answers above it
// aren't parsed as Markdown again for every token (or every keystroke in the composer).
function BubbleView({ bubble: b, onPick }: Props) {
  return (
    <div className={`bubble ${b.role}`}>
      {b.content ? (
        // Users' messages are shown as typed; answers are Markdown (tables, diagrams).
        b.role === "assistant" ? <Markdown text={b.content} streaming={!!b.pending} /> : b.content
      ) : b.pending ? (
        <span className="typing">●●●</span>
      ) : null}
      {b.attachments && b.attachments.length > 0 && (
        <div className="attached-files">
          {b.attachments.map((a) => (
            <span key={a.id} className="file">
              📎 {a.filename}
            </span>
          ))}
        </div>
      )}
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
      {onPick && <Chips bubble={b} onPick={onPick} />}
    </div>
  );
}

export default memo(BubbleView);

function Chips({ bubble, onPick }: { bubble: Bubble; onPick: Pick }) {
  const { kind, items } = chipsFor(bubble);
  if (items.length === 0) return null;
  return (
    <div className="suggestions" aria-label={kind === "answer" ? "답 고르기" : "이어서 물어보기"}>
      {items.map((q) => (
        <button
          key={q}
          type="button"
          className={kind === "answer" ? "chip chip-answer" : "chip"}
          onClick={() => void onPick(q)}
        >
          {q}
        </button>
      ))}
    </div>
  );
}
