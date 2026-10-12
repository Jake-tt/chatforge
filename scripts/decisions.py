"""Decision Docs tooling.

`decision-docs/records.jsonl` holds one decision per line: a durable rule, invariant or
boundary contract, linked to the source paths it governs (`scope_paths`) and the test cases
that prove it (`test_refs`). Agents look decisions up before editing a file, so a change
never silently breaks a rule someone already decided.

    python scripts/decisions.py find --path backend/app/services/claim.py
    python scripts/decisions.py find --test backend/tests/test_claim.py
    python scripts/decisions.py find --query "redis quota"
    python scripts/decisions.py find --changed            # files changed vs main
    python scripts/decisions.py check --strict-paths --strict-names
    python scripts/decisions.py coverage --scope backend --details --limit 30
    python scripts/decisions.py trace-tests --decision-id DEC-BACKEND-GUEST-CLAIM-001

Standard library only, so it runs before any dependency is installed.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

RECORDS = Path("decision-docs") / "records.jsonl"
REQUIRED = ("decision_id", "status", "summary", "scope_paths", "test_refs")
STATUSES = {"inferred", "accepted", "superseded"}
ID_RE = re.compile(r"^DEC(-[A-Z0-9]+)+-\d{3}$")

PY_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.M)
TS_TEST_RE = re.compile(r"\b(?:it|test)\(\s*([\"'`])(.+?)\1", re.M)
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+(app(?:\.\w+)*)\s+import|import\s+(app(?:\.\w+)*))", re.M)
TS_IMPORT_RE = re.compile(r"^\s*import\s+[^\"']*[\"'](\.{1,2}/[^\"']+)[\"']", re.M)


class RecordError(Exception):
    pass


def _norm(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def load_records(root: Path) -> list[dict]:
    path = root / RECORDS
    if not path.exists():
        return []
    records = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path}: line {n}: {exc.msg}") from exc
    return records


# --- lookup -------------------------------------------------------------------

def _covers(scope: str, path: str) -> bool:
    scope = _norm(scope)
    return path == scope or (scope.endswith("/") and path.startswith(scope))


def find_by_path(records: list[dict], path: str) -> list[dict]:
    path = _norm(path)
    return [r for r in records if any(_covers(s, path) for s in r.get("scope_paths", []))]


def find_by_test(records: list[dict], test_path: str) -> list[dict]:
    test_path = _norm(test_path)
    return [r for r in records
            if any(_norm(ref["path"]) == test_path for ref in r.get("test_refs", []))]


def find_by_query(records: list[dict], query: str) -> list[dict]:
    terms = query.casefold().split()

    def haystack(r: dict) -> str:
        refs = " ".join(ref["path"] + " " + " ".join(ref.get("names", []))
                        for ref in r.get("test_refs", []))
        return " ".join([r.get("decision_id", ""), r.get("summary", ""),
                         *r.get("scope_paths", []), refs]).casefold()

    return [r for r in records if all(t in haystack(r) for t in terms)]


def changed_paths(root: Path, base: str) -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        return [_norm(p) for p in out.stdout.splitlines() if p.strip()]

    committed = git("diff", "--name-only", f"{base}...HEAD")
    working = git("diff", "--name-only", "HEAD")
    return sorted(set(committed) | set(working))


# --- tests ----------------------------------------------------------------------

def test_names(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return PY_TEST_RE.findall(text)
    return [m.group(2) for m in TS_TEST_RE.finditer(text)]


def _test_files(root: Path) -> list[Path]:
    return sorted([*root.glob("backend/tests/**/test_*.py"),
                   *root.glob("frontend/src/**/*.test.ts"),
                   *root.glob("frontend/src/**/*.test.tsx")])


# --- check ------------------------------------------------------------------------

def check(records: list[dict], root: Path, strict_paths: bool, strict_names: bool) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for r in records:
        rid = r.get("decision_id", "<missing id>")
        for field in REQUIRED:
            if field not in r:
                problems.append(f"{rid}: missing field '{field}'")
        if not ID_RE.match(rid):
            problems.append(f"{rid}: invalid decision_id (expected DEC-AREA-SLUG-001)")
        if rid in seen:
            problems.append(f"duplicate decision_id {rid}")
        seen.add(rid)
        if r.get("status") not in STATUSES:
            problems.append(f"{rid}: invalid status {r.get('status')!r}")

        if strict_paths:
            for scope in r.get("scope_paths", []):
                if not (root / _norm(scope)).exists():
                    problems.append(f"{rid}: scope_path not found: {scope}")
            for kind in ("doc_refs", "adr_refs"):
                for ref in r.get(kind, []):
                    if not (root / _norm(ref)).exists():
                        problems.append(f"{rid}: {kind[:-1]} not found: {ref}")
        for ref in r.get("test_refs", []):
            test_file = root / _norm(ref["path"])
            if not test_file.exists():
                if strict_paths:
                    problems.append(f"{rid}: test file not found: {ref['path']}")
                continue
            if strict_names:
                existing = set(test_names(test_file))
                for name in ref.get("names", []):
                    if name not in existing:
                        problems.append(f"{rid}: test '{name}' not found in {ref['path']}")
    return problems


# --- coverage / trace -----------------------------------------------------------

def coverage(records: list[dict], root: Path, scope: str | None) -> list[tuple[str, str]]:
    """Test cases that no decision references: candidates for extract-test-decisions."""
    referenced = {(_norm(ref["path"]), name)
                  for r in records for ref in r.get("test_refs", [])
                  for name in ref.get("names", [])}
    uncovered = []
    for path in _test_files(root):
        rel = path.relative_to(root).as_posix()
        if scope and not rel.startswith(_norm(scope)):
            continue
        uncovered += [(rel, n) for n in test_names(path) if (rel, n) not in referenced]
    return uncovered


def _imported_sources(test_file: Path, root: Path) -> list[str]:
    text = test_file.read_text(encoding="utf-8")
    found: list[str] = []
    if test_file.suffix == ".py":
        for a, b in PY_IMPORT_RE.findall(text):
            module = (a or b).replace(".", "/")
            for candidate in (f"backend/{module}.py", f"backend/{module}/__init__.py"):
                if (root / candidate).exists():
                    found.append(candidate)
                    break
    else:
        for spec in TS_IMPORT_RE.findall(text):
            base = (test_file.parent / spec).resolve()
            for candidate in (base.with_suffix(".ts"), base.with_suffix(".tsx"), base):
                if candidate.is_file():
                    found.append(candidate.relative_to(root.resolve()).as_posix())
                    break
    return found


def trace_tests(record: dict, root: Path) -> list[str]:
    """Sources imported by the decision's tests but absent from scope_paths. Advisory only:
    scope_paths are curated by meaning, not copied from the import graph."""
    scoped = [_norm(s) for s in record.get("scope_paths", [])]
    candidates: list[str] = []
    for ref in record.get("test_refs", []):
        test_file = root / _norm(ref["path"])
        if test_file.exists():
            candidates += _imported_sources(test_file, root)
    return sorted({c for c in candidates if not any(_covers(s, c) for s in scoped)})


# --- CLI --------------------------------------------------------------------------

def _print_records(records: list[dict]) -> None:
    if not records:
        print("(no matching decisions)")
    for r in records:
        print(f"{r['decision_id']} [{r.get('status')}]")
        print(f"  {r.get('summary')}")
        for scope in r.get("scope_paths", []):
            print(f"  scope: {scope}")
        for ref in r.get("test_refs", []):
            for name in ref.get("names", []):
                print(f"  test:  {ref['path']}::{name}")
        for ref in [*r.get("doc_refs", []), *r.get("adr_refs", [])]:
            print(f"  doc:   {ref}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decisions", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", help="look up decisions")
    group = find.add_mutually_exclusive_group(required=True)
    group.add_argument("--path")
    group.add_argument("--test")
    group.add_argument("--decision-id")
    group.add_argument("--query")
    group.add_argument("--changed", action="store_true")
    find.add_argument("--base", default="main")

    chk = sub.add_parser("check", help="validate records.jsonl")
    chk.add_argument("--strict-paths", action="store_true")
    chk.add_argument("--strict-names", action="store_true")

    cov = sub.add_parser("coverage", help="test cases no decision references")
    cov.add_argument("--scope")
    cov.add_argument("--details", action="store_true")
    cov.add_argument("--limit", type=int, default=50)
    cov.add_argument("--offset", type=int, default=0)

    trace = sub.add_parser("trace-tests", help="imported sources missing from scope_paths")
    trace.add_argument("--decision-id", required=True)

    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        records = load_records(root)
    except RecordError as exc:
        print(exc)
        return 1

    if args.command == "find":
        if args.path:
            _print_records(find_by_path(records, args.path))
        elif args.test:
            _print_records(find_by_test(records, args.test))
        elif args.decision_id:
            _print_records([r for r in records if r.get("decision_id") == args.decision_id])
        elif args.query:
            _print_records(find_by_query(records, args.query))
        else:
            for path in changed_paths(root, args.base):
                hits = find_by_path(records, path) + find_by_test(records, path)
                if hits:
                    print(f"## {path}")
                    _print_records(hits)
        return 0

    if args.command == "check":
        problems = check(records, root, args.strict_paths, args.strict_names)
        for p in problems:
            print(p)
        print(f"{len(records)} decisions, {len(problems)} problems")
        return 1 if problems else 0

    if args.command == "coverage":
        uncovered = coverage(records, root, args.scope)
        total = sum(len(test_names(p)) for p in _test_files(root)
                    if not args.scope or p.relative_to(root).as_posix().startswith(args.scope))
        print(f"{total - len(uncovered)}/{total} test cases referenced by a decision")
        if args.details:
            for path, name in uncovered[args.offset:args.offset + args.limit]:
                print(f"  {path}::{name}")
        return 0

    record = next((r for r in records if r.get("decision_id") == args.decision_id), None)
    if record is None:
        print(f"unknown decision {args.decision_id}")
        return 1
    candidates = trace_tests(record, root)
    print("\n".join(candidates) if candidates else "(no missing candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
