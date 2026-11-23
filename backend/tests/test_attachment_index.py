"""The cache of built attachment indexes: its bounds, tested on the cache itself (how
long and how much it keeps isn't visible through the API)."""

import uuid

from app.services.attachment_index import AttachmentIndexCache
from app.services.search import BM25Search, Document


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _index(n: int) -> BM25Search:
    return BM25Search([Document(str(i), "t", "u", f"문단 {i}") for i in range(n)])


def _key() -> tuple[uuid.UUID, ...]:
    return (uuid.uuid4(),)


def test_an_index_is_kept_until_it_expires():
    clock = Clock()
    cache = AttachmentIndexCache(max_passages=100, ttl_seconds=60, clock=clock)
    key, index = _key(), _index(3)
    cache.put(key, index, passages=3)

    clock.now = 59
    assert cache.get(key) is index
    clock.now = 60  # counted from when it was built, not last used: a deleted
    assert cache.get(key) is None  # document's text leaves memory within the TTL


def test_the_least_recently_used_index_goes_first_past_the_passage_limit():
    cache = AttachmentIndexCache(max_passages=10, ttl_seconds=60, clock=Clock())
    old, used, new = _key(), _key(), _key()
    cache.put(old, _index(4), passages=4)
    cache.put(used, _index(4), passages=4)
    cache.get(old)  # used again: now the newest

    cache.put(new, _index(4), passages=4)

    assert cache.get(used) is None
    assert cache.get(old) is not None and cache.get(new) is not None


def test_an_index_larger_than_the_whole_cache_is_not_kept():
    cache = AttachmentIndexCache(max_passages=10, ttl_seconds=60, clock=Clock())
    kept, huge = _key(), _key()
    cache.put(kept, _index(4), passages=4)

    cache.put(huge, _index(11), passages=11)

    assert cache.get(huge) is None
    assert cache.get(kept) is not None  # nothing was evicted to make room for it
