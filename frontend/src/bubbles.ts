// How the chat list changes while a reply streams, kept so that bubbles already shown
// keep their props by reference and their memoized rendering is skipped.

/** The list with its last item replaced; every other item is the same object as before. */
export function replaceLast<T>(items: T[], fn: (last: T) => T): T[] {
  return [...items.slice(0, -1), fn(items[items.length - 1])];
}

/** Chips are offered only under the latest bubble, once its reply is done; older ones
 * would be stale. The others get no handler, so a new handler each render can't
 * re-render them. */
export function chipHandler<F>(index: number, count: number, streaming: boolean,
                               onPick: F): F | undefined {
  return index === count - 1 && !streaming ? onPick : undefined;
}
