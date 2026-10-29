import { SSEParser } from "./sse";

const BASE = import.meta.env.VITE_API_URL ?? "/api";
const TOKEN_KEY = "chatforge.token";

export interface Me {
  kind: "user" | "guest";
  id: string;
  plan: string;
  email: string | null;
}
export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}
export interface Citation {
  n: number;
  title: string;
  url: string;
  snippet: string;
  cited?: boolean; // whether the answer cites it; absent until the answer is checked
}
export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  input_tokens: number;
  output_tokens: number;
  citations: Citation[] | null;
  created_at: string;
}
export interface Usage {
  used: number;
  limit: number;
  remaining: number;
  resets_at: string;
}
export interface AuthResult {
  token: string;
  user: { id: string; email: string; plan: string };
  claim: { status: string; conversations_moved: number; tokens_merged: number } | null;
}
export interface StreamDone {
  message_id: string;
  input_tokens: number;
  output_tokens: number;
  stop_reason: string | null;
  cited?: number[]; // source numbers the answer cites
}

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onSources?: (citations: Citation[]) => void;
  onSuggestions?: (questions: string[]) => void;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

function storedToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: session lasts until reload */
  }
  memoryToken = token;
}

let memoryToken: string | null = storedToken();

async function toApiError(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => null);
  const detail = body?.detail;
  if (detail && typeof detail === "object" && "code" in detail) {
    return new ApiError(res.status, detail.code, detail.message);
  }
  return new ApiError(res.status, "http_error", `Request failed (${res.status})`);
}

let pendingGuest: Promise<void> | null = null;

/** Concurrent callers share one request, so parallel first-load calls create one guest. */
function startGuestSession(): Promise<void> {
  pendingGuest ??= (async () => {
    const res = await fetch(`${BASE}/auth/guest`, { method: "POST" });
    if (!res.ok) throw await toApiError(res);
    storeToken((await res.json()).token);
  })().finally(() => {
    pendingGuest = null;
  });
  return pendingGuest;
}

/**
 * fetch with the session token. A 401 (expired token, or a guest session that was merged
 * into an account on another tab) transparently starts a fresh guest session once.
 */
async function authedFetch(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  if (!memoryToken) await startGuestSession();
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
      Authorization: `Bearer ${memoryToken}`,
    },
  });
  if (res.status === 401 && retry) {
    storeToken(null);
    return authedFetch(path, init, false);
  }
  if (!res.ok) throw await toApiError(res);
  return res;
}

const json = async <T>(path: string, init?: RequestInit): Promise<T> =>
  (await authedFetch(path, init)).json();

export const api = {
  me: () => json<Me>("/auth/me"),
  usage: () => json<Usage>("/usage"),
  listConversations: () => json<Conversation[]>("/conversations"),
  getConversation: (id: string) =>
    json<Conversation & { messages: Message[] }>(`/conversations/${id}`),
  createConversation: () =>
    json<Conversation>("/conversations", { method: "POST", body: "{}" }),
  deleteConversation: (id: string) =>
    authedFetch(`/conversations/${id}`, { method: "DELETE" }).then(() => undefined),

  async authenticate(mode: "login" | "signup", email: string, password: string) {
    const me = memoryToken ? await api.me().catch(() => null) : null;
    const res = await fetch(`${BASE}/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        // Hand over the anonymous session so its chats move into the account.
        guest_token: me?.kind === "guest" ? memoryToken : null,
      }),
    });
    if (!res.ok) throw await toApiError(res);
    const result: AuthResult = await res.json();
    storeToken(result.token);
    return result;
  },

  /** Delete the account (password re-checked). The server purges its data in the background. */
  async deleteAccount(password: string) {
    await authedFetch("/auth/me", { method: "DELETE", body: JSON.stringify({ password }) }, false);
    storeToken(null);
  },

  /** Sign out here; with `everywhere`, revoke this account's tokens on every device. */
  async logout(everywhere = false) {
    try {
      if (everywhere) await authedFetch("/auth/logout-all", { method: "POST" }, false);
    } catch (err) {
      // Already signed out (revoked elsewhere, or expired): the goal is met.
      if (!(err instanceof ApiError && err.status === 401)) throw err;
    }
    storeToken(null);
  },

  /** POST a message and stream the reply. Resolves with the `done` payload. */
  async sendMessage(
    conversationId: string,
    content: string,
    handlers: StreamHandlers,
    signal?: AbortSignal,
  ): Promise<StreamDone> {
    const res = await authedFetch(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
      signal,
    });
    const reader = res.body!.getReader();
    const parser = new SSEParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const { event, data } of parser.push(value)) {
        if (event === "delta") handlers.onDelta((data as { text: string }).text);
        else if (event === "sources") {
          handlers.onSources?.((data as { citations: Citation[] }).citations);
        } else if (event === "suggestions") {
          handlers.onSuggestions?.((data as { questions: string[] }).questions);
        }
        else if (event === "done") return data as StreamDone;
        else if (event === "error") {
          const err = data as { code: string; message: string };
          throw new ApiError(502, err.code, err.message);
        }
      }
    }
    throw new ApiError(502, "stream_closed", "Connection closed before the reply finished");
  },
};
