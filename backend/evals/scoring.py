"""Scoring for the agent evaluation (audit A2).

Rule-based by design (decision D4): routing against the labels, expected facts found in the
answer, and citations checked against the sources the turn was given. Faithfulness beyond
that is checked by hand on a sample; a local model judging its own answers would only be a
reference.

Routing is the router's own call per flag. Asking back is scored on what the member saw
(the turn ended with a clarifying question), which also counts when the router failed.

Rates are over the turns they apply to and are None when no turn applies, so "no data" is
never shown as a perfect score.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

from app.services.rag import cited_numbers

FLAGS = ("needs_search", "needs_account", "needs_past", "needs_clarification")


@dataclass
class TurnResult:
    case_id: str
    category: str
    reviewed: bool
    expected: dict[str, bool]
    predicted: dict[str, bool] | None  # None: the router failed (the agent chose itself)
    clarified: bool  # the turn ended with a clarifying question
    answer: str
    sources: list[int]  # numbers of the sources the turn was given
    fact_hits: int
    fact_total: int
    llm_calls: int
    billed_tokens: int
    first_delta_ms: float | None
    total_ms: float
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _normalized(text: str) -> str:
    return re.sub(r"[\s,]", "", text).casefold()


def _found(spelling: str, text: str) -> bool:
    # A number must not be part of a bigger one: "200,000" isn't in "2,000,000", nor
    # "20만" in "120만".
    s = _normalized(spelling)
    pattern = ("(?<!\\d)" if s[:1].isdigit() else "") + re.escape(s) + \
        ("(?!\\d)" if s[-1:].isdigit() else "")
    return re.search(pattern, text) is not None


def fact_hits(answer: str, facts: list[list[str]]) -> tuple[int, int]:
    """(groups found, groups expected): a group is found if any of its spellings is."""
    text = _normalized(answer)
    found = sum(any(_found(s, text) for s in group) for group in facts)
    return found, len(facts)


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest rank: small samples get a value that was actually measured."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _stats(values: list[float]) -> dict:
    return {"mean": sum(values) / len(values) if values else None,
            "p50": _percentile(values, 0.5), "p90": _percentile(values, 0.9)}


def _routing(results: list[TurnResult]) -> dict:
    routing = {}
    for flag in FLAGS:
        tp = fp = fn = 0
        for r in results:
            if flag == "needs_clarification":
                said = r.clarified
            else:
                said = bool(r.predicted and r.predicted[flag])
            want = r.expected[flag]
            tp += said and want
            fp += said and not want
            fn += want and not said
        routing[flag] = {"precision": _rate(tp, tp + fp), "recall": _rate(tp, tp + fn),
                         "tp": tp, "fp": fp, "fn": fn}
    return routing


def summarize(results: list[TurnResult]) -> dict:
    ok = [r for r in results if r.error is None]
    answered = [r for r in ok if not r.clarified]
    with_sources = [r for r in answered if r.sources]
    not_expected = [r for r in ok if not r.expected["needs_clarification"]]
    fact_total = sum(r.fact_total for r in ok)
    return {
        "cases": len(results),
        "errors": len(results) - len(ok),
        "baseline": all(r.reviewed for r in results) and bool(results),
        "router_failures": sum(r.predicted is None for r in ok),
        "routing": _routing(ok),
        "clarify_rate": _rate(sum(r.clarified for r in ok), len(ok)),
        "needless_clarify_rate": _rate(sum(r.clarified for r in not_expected),
                                       len(not_expected)),
        "fact_recall": _rate(sum(r.fact_hits for r in ok), fact_total),
        "missing_citation_rate": _rate(
            sum(bool(cited_numbers(r.answer) - set(r.sources)) for r in answered),
            len(answered)),
        "uncited_with_sources_rate": _rate(
            sum(not cited_numbers(r.answer) for r in with_sources), len(with_sources)),
        "llm_calls": _stats([r.llm_calls for r in ok]),
        "billed_tokens": _stats([r.billed_tokens for r in ok]),
        "first_delta_ms": _stats([r.first_delta_ms for r in ok
                                  if r.first_delta_ms is not None]),
        "total_ms": _stats([r.total_ms for r in ok]),
    }


def _fmt(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def render_markdown(summary: dict, title: str = "에이전트 평가") -> str:
    lines = [f"## {title}", ""]
    if not summary["baseline"]:
        lines += ["> **기준선 아님**: 검토되지 않은 케이스가 섞여 있습니다.", ""]
    lines += [f"케이스 {summary['cases']}건, 오류 {summary['errors']}건, "
              f"라우터 실패 {summary['router_failures']}건", "",
              "| 라우팅 | 정밀도 | 재현율 | TP | FP | FN |", "|---|---|---|---|---|---|"]
    for flag, m in summary["routing"].items():
        lines.append(f"| {flag} | {_fmt(m['precision'])} | {_fmt(m['recall'])} | "
                     f"{m['tp']} | {m['fp']} | {m['fn']} |")
    lines += ["", "| 지표 | 값 |", "|---|---|",
              f"| 되묻기 비율 | {_fmt(summary['clarify_rate'])} |",
              f"| 불필요한 되묻기 비율 | {_fmt(summary['needless_clarify_rate'])} |",
              f"| 기대 사실 포함률 | {_fmt(summary['fact_recall'])} |",
              f"| 없는 번호 인용 비율 | {_fmt(summary['missing_citation_rate'])} |",
              f"| 출처가 있는데 인용 0개 비율 | {_fmt(summary['uncited_with_sources_rate'])} |",
              "", "| 비용·지연 | 평균 | p50 | p90 |", "|---|---|---|---|"]
    for key, label, digits in (("llm_calls", "턴당 LLM 호출", 1),
                               ("billed_tokens", "billed_tokens", 0),
                               ("first_delta_ms", "첫 delta까지(ms)", 0),
                               ("total_ms", "전체 턴(ms)", 0)):
        s = summary[key]
        lines.append(f"| {label} | {_fmt(s['mean'], digits)} | {_fmt(s['p50'], digits)} | "
                     f"{_fmt(s['p90'], digits)} |")
    return "\n".join(lines) + "\n"
