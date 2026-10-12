"""Tests for scripts/decisions.py (Decision Docs tooling)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decisions  # noqa: E402

PY_TEST = '''
async def test_claim_is_idempotent(h):
    pass


def test_breaker_skips_redis():
    pass


def helper_not_a_test():
    pass
'''

TS_TEST = '''
describe("SSEParser", () => {
  it("buffers events split across chunks", () => {});
  test('handles CRLF', () => {});
});
'''


def _record(**overrides) -> dict:
    record = {
        "decision_id": "DEC-BACKEND-CLAIM-001",
        "status": "accepted",
        "summary": "A guest claimed by one user is never re-claimed by another.",
        "scope_paths": ["backend/app/services/claim.py"],
        "test_refs": [{"path": "backend/tests/test_claim.py",
                       "names": ["test_claim_is_idempotent"]}],
        "doc_refs": [],
        "adr_refs": [],
        "source": "new-work",
        "confidence": "high",
    }
    record.update(overrides)
    return record


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in {
        "backend/app/services/claim.py": "from app.models import Guest\n",
        "backend/app/services/quota.py": "",
        "backend/app/models.py": "",
        "backend/tests/test_claim.py": "from app.services.claim import claim_guest\n" + PY_TEST,
        "frontend/src/sse.ts": "",
        "frontend/src/sse.test.ts": 'import { SSEParser } from "./sse";\n' + TS_TEST,
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _write_records(repo: Path, records: list[dict]) -> None:
    path = repo / "decision-docs" / "records.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                    encoding="utf-8")


# --- loading -----------------------------------------------------------------

def test_load_reports_the_line_of_invalid_json(repo):
    (repo / "decision-docs").mkdir()
    (repo / "decision-docs" / "records.jsonl").write_text(
        json.dumps(_record()) + "\n{not json\n", encoding="utf-8"
    )
    with pytest.raises(decisions.RecordError, match="line 2"):
        decisions.load_records(repo)


def test_load_skips_blank_lines(repo):
    _write_records(repo, [_record()])
    path = repo / "decision-docs" / "records.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(decisions.load_records(repo)) == 1


# --- find ----------------------------------------------------------------------

def test_find_by_path_matches_files_and_directories():
    by_file = _record()
    by_dir = _record(decision_id="DEC-BACKEND-SERVICES-001", scope_paths=["backend/app/services/"])
    other = _record(decision_id="DEC-WEB-SSE-001", scope_paths=["frontend/src/sse.ts"])
    records = [by_file, by_dir, other]

    found = decisions.find_by_path(records, "backend/app/services/claim.py")

    assert [r["decision_id"] for r in found] == ["DEC-BACKEND-CLAIM-001",
                                                  "DEC-BACKEND-SERVICES-001"]


def test_find_by_path_normalises_windows_separators():
    assert decisions.find_by_path([_record()], "backend\\app\\services\\claim.py")


def test_find_by_test_uses_test_refs():
    records = [_record(), _record(decision_id="DEC-WEB-SSE-001", test_refs=[
        {"path": "frontend/src/sse.test.ts", "names": ["handles CRLF"]}])]
    assert [r["decision_id"] for r in decisions.find_by_test(records, "frontend/src/sse.test.ts")] \
        == ["DEC-WEB-SSE-001"]


def test_find_by_query_requires_every_term():
    quota = _record(decision_id="DEC-BACKEND-QUOTA-001",
                    summary="Quota is enforced from the database when Redis is down.")
    records = [_record(), quota]
    assert [r["decision_id"] for r in decisions.find_by_query(records, "redis quota")] \
        == ["DEC-BACKEND-QUOTA-001"]
    assert decisions.find_by_query(records, "CLAIM guest")[0]["decision_id"] \
        == "DEC-BACKEND-CLAIM-001"


# --- test discovery ------------------------------------------------------------

def test_discover_python_and_typescript_test_names(repo):
    assert decisions.test_names(repo / "backend/tests/test_claim.py") == [
        "test_claim_is_idempotent", "test_breaker_skips_redis",
    ]
    assert decisions.test_names(repo / "frontend/src/sse.test.ts") == [
        "buffers events split across chunks", "handles CRLF",
    ]


# --- check ---------------------------------------------------------------------

def test_check_passes_for_a_valid_map(repo):
    _write_records(repo, [_record()])
    assert decisions.check(decisions.load_records(repo), repo,
                           strict_paths=True, strict_names=True) == []


def test_check_flags_schema_problems(repo):
    problems = decisions.check([
        _record(decision_id="bad-id"),
        _record(status="maybe"),
        _record(),  # duplicate of the previous id
        {k: v for k, v in _record(decision_id="DEC-BACKEND-X-001").items() if k != "summary"},
    ], repo, strict_paths=False, strict_names=False)

    text = "\n".join(problems)
    assert "bad-id: invalid decision_id" in text
    assert "invalid status 'maybe'" in text
    assert "duplicate decision_id DEC-BACKEND-CLAIM-001" in text
    assert "DEC-BACKEND-X-001: missing field 'summary'" in text


def test_strict_paths_flags_missing_files(repo):
    problems = decisions.check([_record(scope_paths=["backend/app/gone.py"])], repo,
                               strict_paths=True, strict_names=False)
    assert problems == ["DEC-BACKEND-CLAIM-001: scope_path not found: backend/app/gone.py"]


def test_strict_names_flags_renamed_tests(repo):
    record = _record(test_refs=[{"path": "backend/tests/test_claim.py",
                                 "names": ["test_claim_is_idempotent", "test_renamed_away"]}])
    problems = decisions.check([record], repo, strict_paths=True, strict_names=True)
    assert problems == [
        "DEC-BACKEND-CLAIM-001: test 'test_renamed_away' not found in backend/tests/test_claim.py"
    ]


# --- coverage ------------------------------------------------------------------

def test_coverage_lists_tests_no_decision_references(repo):
    uncovered = decisions.coverage([_record()], repo, scope="backend")
    assert uncovered == [("backend/tests/test_claim.py", "test_breaker_skips_redis")]

    all_uncovered = decisions.coverage([_record()], repo, scope=None)
    assert ("frontend/src/sse.test.ts", "handles CRLF") in all_uncovered


# --- trace-tests ---------------------------------------------------------------

def test_trace_tests_suggests_imported_sources_missing_from_scope(repo):
    record = _record(scope_paths=["backend/app/services/quota.py"])
    assert decisions.trace_tests(record, repo) == ["backend/app/services/claim.py"]

    web = _record(decision_id="DEC-WEB-SSE-001", scope_paths=[],
                  test_refs=[{"path": "frontend/src/sse.test.ts", "names": ["handles CRLF"]}])
    assert decisions.trace_tests(web, repo) == ["frontend/src/sse.ts"]


# --- CLI -----------------------------------------------------------------------

def test_cli_check_exit_code(repo, capsys):
    _write_records(repo, [_record(scope_paths=["backend/app/gone.py"])])
    assert decisions.main(["--root", str(repo), "check", "--strict-paths"]) == 1
    assert "scope_path not found" in capsys.readouterr().out

    _write_records(repo, [_record()])
    assert decisions.main(["--root", str(repo), "check", "--strict-paths", "--strict-names"]) == 0


def test_cli_find_prints_summary_and_evidence(repo, capsys):
    _write_records(repo, [_record()])
    assert decisions.main(["--root", str(repo), "find", "--path",
                           "backend/app/services/claim.py"]) == 0
    out = capsys.readouterr().out
    assert "DEC-BACKEND-CLAIM-001" in out
    assert "never re-claimed" in out
    assert "backend/tests/test_claim.py::test_claim_is_idempotent" in out
