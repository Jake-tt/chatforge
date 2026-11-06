import { describe, expect, it } from "vitest";
import type { Citation } from "./api";
import { markCited, shownSources } from "./sources";

const source = (n: number): Citation => ({ n, title: `문서 ${n}`, url: `/help/${n}`, snippet: "" });

describe("shownSources", () => {
  it("lists only the sources the answer cites", () => {
    const marked = markCited([source(1), source(2), source(3)], [1, 3]);
    expect(shownSources(marked).map((c) => c.n)).toEqual([1, 3]);
  });

  it("lists every source when the answer cites none", () => {
    const marked = markCited([source(1), source(2)], []);
    expect(shownSources(marked).map((c) => c.n)).toEqual([1, 2]);
  });

  it("lists every source while the answer is still streaming or was stored unchecked", () => {
    expect(shownSources([source(1), source(2)]).map((c) => c.n)).toEqual([1, 2]);
  });
});

describe("markCited", () => {
  it("leaves sources unmarked when the server sent no citation check", () => {
    // A `done` from a server older than the check has no `cited` list.
    expect(markCited([source(1)], undefined)).toEqual([source(1)]);
  });

  it("treats unmarked sources as cited next to ones marked uncited", () => {
    const mixed = [source(1), { ...source(2), cited: false }];
    expect(shownSources(mixed).map((c) => c.n)).toEqual([1]);
  });
});
