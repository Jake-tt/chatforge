"""The agent evaluation: case format, scoring rules, and one run end to end.

The scoring is what the baseline is made of, so each metric is pinned here with small
hand-checked inputs. The run itself uses fake models: real-model runs are manual and not in
CI (audit A2).
"""

import json

import pytest
from pydantic import ValidationError

from evals.cases import EvalCase, load_cases
from evals.run_agent_eval import run_cases, score_turn
from evals.scoring import TurnResult, fact_hits, render_markdown, summarize
from tests.fakes import RecordingChatModel

FLAGS = ("needs_search", "needs_account", "needs_past", "needs_clarification")


def _case(**overrides) -> dict:
    case = {"id": "help-1", "category": "help", "turns": ["무료 요금제 한도는?"],
            "expect": {"needs_search": True, "facts": [["200,000", "20만"]]}}
    return {**case, **overrides}


def _result(case_id="c", *, expected=None, predicted=None, clarified=False, answer="",
            sources=(), facts=(0, 0), calls=3, billed=100, first=500.0, total=1000.0,
            error=None, reviewed=True, category="help") -> TurnResult:
    return TurnResult(
        case_id=case_id, category=category, reviewed=reviewed,
        expected={f: f in (expected or ()) for f in FLAGS},
        predicted=None if predicted is None else {f: f in predicted for f in FLAGS},
        clarified=clarified, answer=answer, sources=list(sources),
        fact_hits=facts[0], fact_total=facts[1], llm_calls=calls, billed_tokens=billed,
        first_delta_ms=first, total_ms=total, error=error)


# --- case format ---------------------------------------------------------------------

def test_a_case_needs_a_known_category_turns_and_real_fact_groups():
    EvalCase.model_validate(_case())
    for bad in (_case(category="pubmed"), _case(turns=[]),
                _case(expect={"facts": [[]]}), _case(extra="field")):
        with pytest.raises(ValidationError):
            EvalCase.model_validate(bad)


def test_case_ids_must_be_unique(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(_case()) for _ in range(2)), encoding="utf-8")
    with pytest.raises(ValueError, match="help-1"):
        load_cases(path)


def test_the_committed_cases_are_valid():
    from evals.run_agent_eval import CASES

    assert load_cases(CASES)


# --- scoring -------------------------------------------------------------------------

def test_routing_precision_and_recall_per_flag():
    summary = summarize([
        _result("tp", expected={"needs_search"}, predicted={"needs_search"}),
        _result("fn", expected={"needs_search"}, predicted=set()),
        _result("fp", expected=set(), predicted={"needs_search"}),
        _result("tn", expected=set(), predicted=set()),
    ])

    search = summary["routing"]["needs_search"]
    assert (search["precision"], search["recall"]) == (0.5, 0.5)
    # Nothing expected or predicted for a flag: undefined, not perfect.
    assert summary["routing"]["needs_past"] == {"precision": None, "recall": None,
                                                 "tp": 0, "fp": 0, "fn": 0}


def test_a_failed_router_counts_as_predicting_nothing():
    summary = summarize([_result(expected={"needs_search"}, predicted=None)])

    assert summary["routing"]["needs_search"]["recall"] == 0.0
    assert summary["router_failures"] == 1


def test_clarifying_is_scored_as_its_own_flag_and_as_a_rate():
    summary = summarize([
        _result("ok", expected={"needs_clarification"}, clarified=True),
        _result("needless", expected=set(), clarified=True),
        _result("plain", expected=set()),
        _result("plain2", expected=set()),
    ])

    assert summary["routing"]["needs_clarification"]["precision"] == 0.5
    assert summary["clarify_rate"] == 0.5
    assert summary["needless_clarify_rate"] == pytest.approx(1 / 3)


def test_expected_facts_match_alternatives_regardless_of_spacing_and_case():
    assert fact_hits("무료는 하루 200,000 토큰", [["200,000", "20만"], ["KST"]]) == (1, 2)
    assert fact_hits("한도는 20 만 토큰, kst 자정 초기화", [["200,000", "20만"], ["KST"]]) == (2, 2)


def test_a_number_is_not_found_inside_a_bigger_one():
    assert fact_hits("프로는 2,000,000 토큰", [["200,000"]]) == (0, 1)
    assert fact_hits("120만 토큰", [["20만"]]) == (0, 1)
    assert fact_hits("하루 200,000토큰", [["200,000"]]) == (1, 1)


def test_citation_metrics_count_answers_not_citations():
    summary = summarize([
        _result("good", answer="한도는 200,000입니다 [1].", sources=[1, 2]),
        _result("ghost", answer="출처 [3]에 따르면", sources=[1]),
        _result("uncited", answer="한도는 200,000입니다.", sources=[1]),
        _result("no-sources", answer="안녕하세요"),
        _result("asked", answer="어떤 요금제요?", sources=[1], clarified=True),
    ])

    assert summary["missing_citation_rate"] == pytest.approx(1 / 4)
    assert summary["uncited_with_sources_rate"] == pytest.approx(1 / 3)


def test_fact_recall_pools_every_expected_fact():
    summary = summarize([_result("a", facts=(1, 2)), _result("b", facts=(2, 2)),
                         _result("none", facts=(0, 0))])

    assert summary["fact_recall"] == 0.75


def test_cost_and_latency_skip_turns_without_a_measurement():
    summary = summarize([_result("a", calls=2, first=100.0, total=1000.0),
                         _result("b", calls=4, first=None, total=3000.0),
                         _result("err", error="timeout", calls=9, total=9999.0)])

    assert summary["llm_calls"]["mean"] == 3
    assert summary["first_delta_ms"]["p50"] == 100.0
    assert summary["total_ms"]["p90"] == 3000.0
    assert summary["errors"] == 1


def test_a_run_with_unreviewed_cases_is_not_a_baseline():
    summary = summarize([_result("a"), _result("b", reviewed=False)])

    assert summary["baseline"] is False
    assert "기준선 아님" in render_markdown(summary)
    assert "기준선 아님" not in render_markdown(summarize([_result("a")]))


# --- reading a turn's stream ------------------------------------------------------------

def _turn(*events, status=200, first=100.0, total=900.0) -> dict:
    return {"status": status, "events": list(events), "first_delta_ms": first,
            "total_ms": total}


def test_a_turn_is_read_from_its_events():
    case = EvalCase.model_validate(_case())
    turn = _turn(("sources", {"citations": [{"n": 1}]}),
                 ("sources", {"citations": [{"n": 1}, {"n": 2}]}),
                 ("delta", {"text": "하루 200,000 "}), ("delta", {"text": "토큰 [1]"}),
                 ("done", {"billed_tokens": 321}))

    result = score_turn(case, [turn], predicted={"needs_search": True}, llm_calls=3)

    assert (result.answer, result.sources, result.billed_tokens) == ("하루 200,000 토큰 [1]",
                                                                      [1, 2], 321)
    assert (result.fact_hits, result.error) == (1, None)


def test_a_router_failure_and_a_failed_turn_are_kept_apart():
    case = EvalCase.model_validate(_case(turns=["첫 질문", "두번째"]))
    ok = _turn(("delta", {"text": "답"}), ("done", {"billed_tokens": 1}))

    failed_router = score_turn(case, [ok, ok], predicted=None, llm_calls=2)
    stream_error = score_turn(case, [ok, _turn(("error", {"message": "model down"}))],
                              predicted={}, llm_calls=2)
    http_error = score_turn(case, [ok, _turn(status=429)], predicted={}, llm_calls=0)

    assert failed_router.predicted is None and failed_router.error is None
    assert stream_error.error == "model down" and http_error.error == "HTTP 429"


def test_a_broken_earlier_turn_fails_the_case():
    # The scored turn would otherwise be judged on a history that never happened.
    case = EvalCase.model_validate(_case(turns=["첫 질문", "두번째"]))
    ok = _turn(("delta", {"text": "답"}), ("done", {"billed_tokens": 1}))

    result = score_turn(case, [_turn(status=500), ok], predicted={}, llm_calls=1)

    assert result.error == "turn 1: HTTP 500"


# --- one run, end to end (fake models) ---------------------------------------------------

async def test_a_run_drives_each_case_through_the_api_and_scores_it(make_settings):
    cases = [EvalCase.model_validate(c) for c in (
        _case(),
        _case(id="past-1", category="past", turns=["지난번 부산 숙소 어디로 했지?"],
              setup={"past": [["부산 여행 숙소는 해운대 근처로 정했어"]]},
              expect={"needs_past": True, "facts": [["해운대"]]}),
        _case(id="memory-1", category="memory", turns=["나 뭐 좋아하는지 알아?", "음료는?"],
              setup={"memory": [{"subject": "나", "predicate": "좋아하는 음료",
                                 "object": "라떼"}]},
              expect={"facts": [["라떼"]]}),
        # Several of each to set up: one database, so none of it may wait on another.
        _case(id="seed-many", category="past", turns=["예전 얘기 기억나?"],
              setup={"past": [["첫 대화"], ["두번째 대화", "더 있음"]],
                     "memory": [{"subject": "나", "predicate": "사는 곳", "object": "부산"},
                                {"subject": "나", "predicate": "직업", "object": "개발자"}]},
              expect={"needs_past": True}),
    )]

    answers = RecordingChatModel(script=[])
    results = await run_cases(cases, make_settings(rag_enabled=True, suggestions_enabled=True),
                              chat_model=answers)

    assert [r.case_id for r in results] == ["help-1", "past-1", "memory-1", "seed-many"]
    assert all(r.error is None for r in results)
    assert all(r.predicted is not None for r in results)  # the router's log was captured
    assert all(r.llm_calls >= 1 and r.billed_tokens > 0 and r.total_ms > 0 for r in results)
    # Only the last turn is scored: the multi-turn case is the answer to its second message.
    [call] = [i for i in range(len(answers.calls)) if answers.contents(i)[-1].endswith("음료는?")]
    assert results[2].answer == f"reply {call + 1}"
    summary = summarize(results)
    assert summary["cases"] == 4
    assert "| needs_search |" in render_markdown(summary)
