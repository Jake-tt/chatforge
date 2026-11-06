import type { Citation } from "./api";

/** The sources to list under an answer: the ones it cites, or every source it was given
 * when it cites none (or before the answer has finished and been checked). */
export function shownSources(citations: Citation[]): Citation[] {
  const cited = citations.filter((c) => c.cited !== false);
  return cited.length > 0 ? cited : citations;
}

/** Mark each source with whether the finished answer cites it (from the `done` event).
 * Without a `cited` list (an older server) the sources stay unmarked: all shown. */
export function markCited(citations: Citation[], cited: number[] | undefined): Citation[] {
  if (!cited) return citations;
  return citations.map((c) => ({ ...c, cited: cited.includes(c.n) }));
}
