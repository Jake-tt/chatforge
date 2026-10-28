"""PMC full-text collector for the PubMed evaluation: parsing, caching, rate limiting.

NCBI is the external boundary: tests pass a fake `get` and `sleep`, so no request leaves
the process and no time passes.
"""

import json

from evals.fetch_pmc import Collector, parse_bioc

BIOC = [{"documents": [{"infons": {"license": "CC BY"}, "passages": [
    {"infons": {"section_type": "TITLE"}, "text": "Grapefruit and drugs"},
    {"infons": {"section_type": "ABSTRACT"}, "text": "We studied ..."},
    {"infons": {"section_type": "RESULTS"}, "text": "Levels rose."},
    {"infons": {"section_type": "RESULTS"}, "text": "Markedly."},
    {"infons": {"section_type": "REF"}, "text": "[1] Smith"},
    {"infons": {"section_type": "CONCL"}, "text": "Avoid it."},
]}]}]


def test_bioc_is_parsed_into_sections_without_references():
    paper = parse_bioc("PMC1", json.dumps(BIOC))

    assert paper["license"] == "CC BY"
    assert paper["title"] == "Grapefruit and drugs"
    assert [s["type"] for s in paper["sections"]] == ["ABSTRACT", "RESULTS", "CONCL"]
    assert paper["sections"][1]["text"] == "Levels rose.\nMarkedly."


def test_a_non_open_access_article_parses_to_none():
    assert parse_bioc("PMC1", "[Error] : No result can be found.") is None


class FakeNcbi:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.urls.append(url)
        return self.responses.pop(0)


def test_each_article_is_requested_once_across_runs(tmp_path):
    ncbi = FakeNcbi((200, json.dumps(BIOC)))
    sleeps: list[float] = []
    collector = Collector(tmp_path, get=ncbi, sleep=sleeps.append, interval=3.0)

    first = collector.paper("PMC1")
    again = Collector(tmp_path, get=ncbi, sleep=sleeps.append, interval=3.0).paper("PMC1")

    assert first == again and first["sections"]
    assert len(ncbi.urls) == 1  # the second run read the cache
    assert "email" not in ncbi.urls[0]


def test_requests_are_spaced_and_rate_limits_back_off(tmp_path):
    ncbi = FakeNcbi((429, "Too Many Requests"), (200, json.dumps(BIOC)),
                    (200, "[Error] : No result can be found."))
    sleeps: list[float] = []
    collector = Collector(tmp_path, get=ncbi, sleep=sleeps.append, interval=3.0)

    assert collector.paper("PMC1")["sections"]
    assert collector.paper("PMC2") is None

    # 429 → wait longer and retry; every request is at least `interval` apart.
    assert sleeps[0] >= 3.0 and max(sleeps) > 3.0
    assert len(ncbi.urls) == 3
    # A "not open access" answer is cached too, so it is never asked again.
    assert Collector(tmp_path, get=ncbi, sleep=sleeps.append).paper("PMC2") is None
    assert len(ncbi.urls) == 3
