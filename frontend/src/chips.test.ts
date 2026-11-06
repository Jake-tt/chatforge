import { describe, expect, it } from "vitest";
import { chipsFor, composerHint } from "./chips";

describe("chipsFor", () => {
  it("offers a clarifying question's options as answers", () => {
    expect(chipsFor({ options: ["프로", "팀"] })).toEqual({ kind: "answer", items: ["프로", "팀"] });
  });

  it("offers no chips when a clarifying question expects free text", () => {
    expect(chipsFor({ options: [] })).toEqual({ kind: "answer", items: [] });
  });

  it("offers follow-up questions under an answer", () => {
    expect(chipsFor({ options: null, suggestions: ["예시를 들어줄래?"] })).toEqual({
      kind: "followup",
      items: ["예시를 들어줄래?"],
    });
  });

  it("offers nothing under an answer without suggestions", () => {
    expect(chipsFor({})).toEqual({ kind: "followup", items: [] });
  });
});

describe("composerHint", () => {
  const plain = "메시지를 입력하세요 (Shift+Enter 줄바꿈)";

  it("asks for an answer after a clarifying question", () => {
    expect(composerHint({ role: "assistant", options: ["프로"] })).toBe(
      "위에서 고르거나 직접 입력해 주세요",
    );
    expect(composerHint({ role: "assistant", options: [] })).toBe("답을 입력해 주세요");
  });

  it("is the usual prompt otherwise", () => {
    expect(composerHint({ role: "assistant", options: null })).toBe(plain);
    expect(composerHint(undefined)).toBe(plain);
  });
});
