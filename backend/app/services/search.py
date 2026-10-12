"""Knowledge-base retrieval.

`SearchProvider` is the seam for a managed search service (e.g. Vertex AI Search). The
bundled `BM25Search` indexes the markdown help center in `knowledge/` in memory, which is
enough for a few hundred documents and needs no infrastructure.

Korean has no spaces between morphemes in many compounds ("토큰한도", "한도를"), so Hangul
runs are indexed as character bigrams; that matches "한도" inside "한도를" without a
morphological analyser. Latin words and numbers are indexed whole.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_TOKEN_RE = re.compile(r"[0-9a-z]+|[가-힣]+")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _TOKEN_RE.findall(text.lower()):
        if "가" <= word[0] <= "힣" and len(word) > 1:
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.append(word)
    return tokens


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    url: str
    text: str


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    title: str
    url: str
    text: str
    score: float


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[SearchHit]: ...


class BM25Search:
    def __init__(self, documents: list[Document], k1: float = 1.5, b: float = 0.75,
                 relative_cutoff: float = 0.3, min_coverage: float = 0.4):
        self._docs = documents
        self._k1, self._b = k1, b
        # Drop hits scoring under this fraction of the best one: they add noise (and
        # tokens) to the prompt.
        self._cutoff = relative_cutoff
        # Require this share of the query's distinct terms in the document. Without it a
        # single shared bigram ("오늘" in "오늘 날씨 어때") is enough to "ground" small talk.
        self._min_coverage = min_coverage
        self._tfs = [Counter(tokenize(f"{d.title} {d.text}")) for d in documents]
        self._lens = [sum(tf.values()) for tf in self._tfs]
        self._avg_len = (sum(self._lens) / len(self._lens)) if documents else 0.0
        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        n = len(documents)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        terms = set(tokenize(query))
        scored = []
        for i, tf in enumerate(self._tfs):
            matched = terms & tf.keys()
            if not terms or len(matched) / len(terms) < self._min_coverage:
                continue
            score = 0.0
            norm = self._k1 * (1 - self._b + self._b * self._lens[i] / self._avg_len)
            for term in matched:
                f = tf[term]
                score += self._idf[term] * f * (self._k1 + 1) / (f + norm)
            if score > 0:
                scored.append((score, i))
        if not scored:
            return []
        scored.sort(reverse=True)
        best = scored[0][0]
        return [
            SearchHit(self._docs[i].id, self._docs[i].title, self._docs[i].url,
                      self._docs[i].text, round(score, 3))
            for score, i in scored[:limit]
            if score >= best * self._cutoff
        ]


def load_markdown_corpus(directory: str | Path) -> list[Document]:
    """One document per `## ` section of each markdown file.

    Files start with a small front matter block:  ---\\ntitle: …\\nurl: …\\n---
    """
    documents: list[Document] = []
    for path in sorted(Path(directory).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = raw
        if raw.startswith("---"):
            header, body = raw[3:].split("---", 1)
            for line in header.strip().splitlines():
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        page_title = meta.get("title", path.stem)
        url = meta.get("url", "")
        for n, section in enumerate(re.split(r"^## ", body, flags=re.M)[1:], start=1):
            heading, _, text = section.partition("\n")
            documents.append(Document(
                id=f"{path.stem}#{n}",
                title=f"{page_title} › {heading.strip()}",
                url=url,
                text=" ".join(text.split()),
            ))
    return documents
