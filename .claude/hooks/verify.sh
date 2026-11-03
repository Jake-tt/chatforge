#!/usr/bin/env bash
# Stop hook: Claude가 턴을 끝내기 전에 바뀐 영역의 게이트를 실행한다.
# 실패하면 exit 2로 종료를 막고, 실패 출력을 stderr로 Claude에게 돌려준다.
# 같은 작업 트리 상태로 이미 통과했으면 다시 실행하지 않는다(대화만 하는 턴에 비용을 쓰지 않기 위해).
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 0

changed=$(
  {
    git diff --name-only HEAD
    git ls-files --others --exclude-standard
    git diff --name-only main...HEAD 2>/dev/null
  } | sort -u
)
[ -z "$changed" ] && exit 0

has() { grep -qE "$1" <<<"$changed"; }

state=$(
  {
    git rev-parse HEAD
    git diff HEAD
    git ls-files --others --exclude-standard -z | xargs -0 shasum 2>/dev/null
  } | shasum | cut -d' ' -f1
)
stamp="$(git rev-parse --git-dir)/claude-verified"
[ "$(cat "$stamp" 2>/dev/null)" = "$state" ] && exit 0

failed=0
run() {
  local label=$1
  shift
  local out
  if ! out=$("$@" 2>&1); then
    printf '[%s] 실패\n%s\n\n' "$label" "$(tail -n 40 <<<"$out")" >&2
    failed=1
  fi
}

if has '^(backend/|scripts/|decision-docs/)'; then
  run "decisions check" python3 scripts/decisions.py check --strict-paths --strict-names
fi

if has '^backend/'; then
  if [ -x backend/.venv/bin/pytest ]; then
    run "ruff" bash -c 'cd backend && .venv/bin/ruff check .'
    run "pytest" bash -c 'cd backend && .venv/bin/pytest -q -x'
  else
    echo "backend/.venv가 없어 백엔드 게이트를 건너뜀" >&2
  fi
fi

if has '^frontend/'; then
  if [ -d frontend/node_modules ]; then
    run "frontend" bash -c 'cd frontend && npm run typecheck --silent && npm test --silent'
  else
    echo "frontend/node_modules가 없어 프론트 게이트를 건너뜀" >&2
  fi
fi

if [ "$failed" -ne 0 ]; then
  echo "게이트를 통과할 때까지 고친 뒤 끝내세요. 규칙을 끄거나 테스트를 skip하지 마세요." >&2
  exit 2
fi

echo "$state" >"$stamp"
exit 0
