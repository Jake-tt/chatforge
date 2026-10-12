import { Conversation, Me, Usage } from "../api";

interface Props {
  me: Me | null;
  usage: Usage | null;
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onLogin: () => void;
  onLogout: () => void;
}

export default function Sidebar(props: Props) {
  const { me, usage, conversations, activeId } = props;
  const pct = usage ? Math.min(100, Math.round((usage.used / usage.limit) * 100)) : 0;

  return (
    <aside className="sidebar">
      <div className="brand">Chatforge</div>
      <button className="btn btn-primary" onClick={props.onNew}>
        + 새 대화
      </button>

      <nav className="conv-list">
        {conversations.length === 0 && <p className="muted">아직 대화가 없어요.</p>}
        {conversations.map((c) => (
          <div
            key={c.id}
            className={`conv-item ${c.id === activeId ? "active" : ""}`}
            onClick={() => props.onSelect(c.id)}
          >
            <span className="conv-title">{c.title ?? "새 대화"}</span>
            <button
              className="icon-btn"
              aria-label="대화 삭제"
              onClick={(e) => {
                e.stopPropagation();
                props.onDelete(c.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        {usage && (
          <div className="usage" title={`초기화: ${new Date(usage.resets_at).toLocaleString()}`}>
            <div className="usage-label">
              <span>오늘 사용량</span>
              <span>
                {usage.used.toLocaleString()} / {usage.limit.toLocaleString()}
              </span>
            </div>
            <div className="meter">
              <div className={`meter-fill ${pct >= 90 ? "warn" : ""}`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        )}
        {me?.kind === "user" ? (
          <div className="account">
            <span className="muted">{me.email}</span>
            <button className="btn btn-ghost" onClick={props.onLogout}>
              로그아웃
            </button>
          </div>
        ) : (
          <button className="btn" onClick={props.onLogin}>
            로그인 / 가입
          </button>
        )}
      </div>
    </aside>
  );
}
