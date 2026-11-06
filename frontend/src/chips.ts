// Chips under the latest assistant message: answers to a clarifying question, or
// follow-up questions under an answer. `options` is set (possibly empty) only on a
// clarifying question; an empty list means the server expects a typed answer.

interface Chippable {
  options?: string[] | null;
  suggestions?: string[];
}

export function chipsFor(b: Chippable): { kind: "answer" | "followup"; items: string[] } {
  if (b.options != null) return { kind: "answer", items: b.options };
  return { kind: "followup", items: b.suggestions ?? [] };
}

export function composerHint(last: { role: string; options?: string[] | null } | undefined): string {
  if (last?.role === "assistant" && last.options != null) {
    return last.options.length > 0 ? "위에서 고르거나 직접 입력해 주세요" : "답을 입력해 주세요";
  }
  return "메시지를 입력하세요 (Shift+Enter 줄바꿈)";
}
