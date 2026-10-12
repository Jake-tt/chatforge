import { FormEvent, useState } from "react";
import { api, ApiError } from "../api";

interface Props {
  onClose: () => void;
  onAuthenticated: (conversationsMoved: number) => void;
}

export default function AuthDialog({ onClose, onAuthenticated }: Props) {
  const [mode, setMode] = useState<"login" | "signup">("signup");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.authenticate(mode, email, password);
      onAuthenticated(result.claim?.status === "claimed" ? result.claim.conversations_moved : 0);
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setError("이메일 형식과 8자 이상의 비밀번호를 확인해 주세요.");
      } else {
        setError(err instanceof ApiError ? err.message : "문제가 발생했어요.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="overlay" onClick={onClose}>
      <form className="dialog" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="tabs">
          <button type="button" className={mode === "signup" ? "active" : ""} onClick={() => setMode("signup")}>
            가입
          </button>
          <button type="button" className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>
            로그인
          </button>
        </div>
        <p className="muted">게스트로 나눈 대화는 계정으로 자동으로 옮겨져요.</p>
        <label>
          이메일
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </label>
        <label>
          비밀번호
          <input
            type="password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
          />
        </label>
        {error && <div className="banner banner-error">{error}</div>}
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "처리 중…" : mode === "signup" ? "가입하기" : "로그인"}
        </button>
      </form>
    </div>
  );
}
