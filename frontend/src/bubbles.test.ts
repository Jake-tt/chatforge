import { describe, expect, it } from "vitest";
import { chipHandler, replaceLast } from "./bubbles";
import BubbleView from "./components/BubbleView";

// A delta re-renders the chat; an answer already shown must keep the same props, so its
// memoized bubble (and its Markdown) isn't rendered again for every token.
describe("rendering a streamed reply", () => {
  it("changes only the last bubble and keeps the others as they were", () => {
    const earlier = [{ content: "질문" }, { content: "긴 답변" }];
    const before = [...earlier, { content: "스트리" }];
    const after = replaceLast(before, (b) => ({ ...b, content: b.content + "밍" }));

    expect(after).toEqual([...earlier, { content: "스트리밍" }]);
    after.slice(0, -1).forEach((b, i) => expect(b).toBe(before[i]));
  });

  it("gives the chip handler only to the last bubble, once the reply is done", () => {
    const pick = async () => undefined;
    expect(chipHandler(0, 3, false, pick)).toBeUndefined();
    expect(chipHandler(2, 3, true, pick)).toBeUndefined();
    expect(chipHandler(2, 3, false, pick)).toBe(pick);
  });

  it("skips rendering a bubble whose props didn't change", () => {
    expect((BubbleView as unknown as { $$typeof: symbol }).$$typeof).toBe(
      Symbol.for("react.memo"),
    );
  });
});
