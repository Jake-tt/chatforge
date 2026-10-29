import { FormEvent, useState } from "react";
import { api, ApiError } from "../api";

interface Props {
  onClose: () => void;
  onDeleted: () => void;
}

export default function DeleteAccountDialog({ onClose, onDeleted }: Props) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.deleteAccount(password);
      onDeleted();
    } catch (err) {
      if (err instanceof ApiError && err.code === "invalid_credentials") {
        setError("비밀번호가 맞지 않아요.");
      } else if (err instanceof ApiError && err.code === "rate_limited") {
        setError("시도가 너무 많아요. 잠시 후 다시 시도해 주세요.");
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
        <h2>계정 삭제</h2>
        <p className="muted">
          모든 대화와 사용 기록이 지워지고, 모든 기기에서 로그아웃돼요. 되돌릴 수 없어요.
        </p>
        <label>
          비밀번호 확인
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            autoFocus
          />
        </label>
        {error && <div className="banner banner-error">{error}</div>}
        <button type="submit" className="btn btn-danger" disabled={busy}>
          {busy ? "삭제 중…" : "계정 삭제"}
        </button>
      </form>
    </div>
  );
}
