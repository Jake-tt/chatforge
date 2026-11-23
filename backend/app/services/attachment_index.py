"""Built search indexes of a conversation's cut documents, kept for the next search.

Splitting and tokenizing is nearly all of a search's cost (four 200K-character documents:
~130 ms of CPU on the event loop, against ~1.4 ms for the query itself), and an
attachment's markdown never changes once sent. So the index is built once per set of
documents and reused, per instance, without a database read of the text.

The key is the sorted ids of the conversation's cut documents: a document cut later makes
a new key, so a stale index is never served. Entries leave by age (counted from when
they were built, so a deleted document's text is out of memory within the TTL) and by
least recent use once the passages held pass the limit.
"""

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from app.services.search import BM25Search

Key = tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _Entry:
    index: BM25Search
    passages: int
    built_at: float


class AttachmentIndexCache:
    def __init__(self, max_passages: int, ttl_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self._max_passages = max_passages
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[Key, _Entry] = OrderedDict()
        self._held = 0  # passages across all entries

    def get(self, key: Key) -> BM25Search | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.built_at >= self._ttl:
            self._drop(key)
            return None
        self._entries.move_to_end(key)
        return entry.index

    def put(self, key: Key, index: BM25Search, passages: int) -> None:
        if passages > self._max_passages:
            return  # it would push out everything else and still not fit
        if key in self._entries:
            self._drop(key)
        self._entries[key] = _Entry(index, passages, self._clock())
        self._held += passages
        while self._held > self._max_passages:
            self._drop(next(iter(self._entries)))

    def _drop(self, key: Key) -> None:
        self._held -= self._entries.pop(key).passages
