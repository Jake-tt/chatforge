"""Collect PubMedQA questions and the open-access full text of their papers.

    python -m evals.fetch_pmc            # from backend/, writes evals/data/

PubMedQA (MIT) gives 1,000 questions, each with its source paper (PMID) and a yes/no/maybe
answer. Papers with a PMC open-access copy are fetched as BioC JSON and kept as sections
(abstract, methods, results, discussion, conclusion), which the evaluation chunks.

NCBI limits request rates, so the collector:
- sleeps `--interval` seconds before every request and backs off on 429,
- caches every answer on disk, "not open access" included, so a rerun asks nothing twice,
- sends no email or other personal parameter.

The data is downloaded at run time and never committed: abstracts and articles keep their
publishers' licenses.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from pathlib import Path

PUBMEDQA_URL = "https://raw.githubusercontent.com/pubmedqa/pubmedqa/master/data/ori_pqal.json"
IDCONV_URL = ("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
              "?ids={ids}&format=json&tool=chatforge-eval")
BIOC_URL = ("https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/"
            "BioC_json/{pmcid}/unicode")
IDCONV_BATCH = 200
# Not prose a question can be answered from.
SKIP_SECTIONS = {"REF", "FIG", "TABLE", "AUTH_CONT", "ACK_FUND", "COMP_INT", "SUPPL",
                 "ABBR", "REVIEW_INFO", "TITLE"}

Get = Callable[[str], tuple[int, str]]


def http_get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def parse_bioc(pmcid: str, raw: str) -> dict | None:
    """Sections of one article, consecutive passages of a section joined; None when the
    service has no open-access text for it."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    collection = data[0] if isinstance(data, list) else data
    document = collection["documents"][0]
    sections: list[dict] = []
    title = ""
    for passage in document["passages"]:
        kind = passage.get("infons", {}).get("section_type", "")
        text = passage.get("text", "").strip()
        if kind == "TITLE" and not title:
            title = text
        if not text or kind in SKIP_SECTIONS:
            continue
        if sections and sections[-1]["type"] == kind:
            sections[-1]["text"] += "\n" + text
        else:
            sections.append({"type": kind, "text": text})
    return {"pmcid": pmcid, "license": document.get("infons", {}).get("license"),
            "title": title, "sections": sections}


class Collector:
    def __init__(self, cache_dir: Path, get: Get = http_get,
                 sleep: Callable[[float], None] = time.sleep, interval: float = 3.0,
                 max_retries: int = 5):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._get = get
        self._sleep = sleep
        self._interval = interval
        self._max_retries = max_retries

    def _request(self, url: str) -> tuple[int, str]:
        for attempt in range(self._max_retries + 1):
            # Wait before every request, longer after each 429.
            self._sleep(self._interval * (2 ** attempt))
            status, body = self._get(url)
            if status != 429:
                return status, body
        raise RuntimeError(f"still rate limited after {self._max_retries} retries: {url}")

    def paper(self, pmcid: str) -> dict | None:
        cached = self._dir / "papers" / f"{pmcid}.json"
        if cached.exists():
            return json.loads(cached.read_text())["paper"]
        status, body = self._request(BIOC_URL.format(pmcid=pmcid))
        paper = parse_bioc(pmcid, body) if status == 200 else None
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"paper": paper}, ensure_ascii=False))
        return paper

    def pmcids(self, pmids: list[str]) -> dict[str, str]:
        cached = self._dir / "idconv.json"
        if cached.exists():
            return json.loads(cached.read_text())
        mapping: dict[str, str] = {}
        for i in range(0, len(pmids), IDCONV_BATCH):
            status, body = self._request(IDCONV_URL.format(ids=",".join(pmids[i:i + IDCONV_BATCH])))
            if status != 200:
                raise RuntimeError(f"idconv failed with HTTP {status}")
            for record in json.loads(body).get("records", []):
                if record.get("pmcid"):
                    mapping[record["pmid"]] = record["pmcid"]
        cached.write_text(json.dumps(mapping))
        return mapping


def load_pubmedqa(cache_dir: Path) -> dict:
    path = cache_dir / "ori_pqal.json"
    if not path.exists():
        status, body = http_get(PUBMEDQA_URL)
        if status != 200:
            raise RuntimeError(f"PubMedQA download failed with HTTP {status}")
        path.write_text(body)
    return json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="seconds to wait before each NCBI request")
    args = parser.parse_args()

    questions = load_pubmedqa(args.out)
    collector = Collector(args.out, interval=args.interval)
    mapping = collector.pmcids(list(questions))
    print(f"{len(questions)} questions, {len(mapping)} papers in PMC")

    rows, sections = [], Counter()
    for n, (pmid, pmcid) in enumerate(mapping.items(), start=1):
        paper = collector.paper(pmcid)
        print(f"[{n}/{len(mapping)}] {pmcid}: "
              f"{'open access' if paper and paper['sections'] else 'no full text'}")
        if not (paper and paper["sections"]):
            continue
        sections.update(s["type"] for s in paper["sections"])
        q = questions[pmid]
        rows.append({"pmid": pmid, "question": q["QUESTION"], "answer": q["final_decision"],
                     "abstract": q["CONTEXTS"], **paper})

    corpus = args.out / "corpus.jsonl"
    corpus.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"\n{len(rows)} questions with open-access full text → {corpus}")
    print("sections:", dict(sections.most_common()))


if __name__ == "__main__":
    main()
