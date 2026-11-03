#!/usr/bin/env bash
# PreToolUse(Bash) hook: 과금되는 명령은 모델 판단과 무관하게 사람에게 승인을 묻는다.
# 권한 규칙은 명령 앞부분만 보고, 환경변수 값(LLM_PROVIDER=anthropic)이나
# 실행 형태(bash deploy/x.sh, ./deploy/x.sh)를 모두 잡지 못해 hook으로 판정한다.
set -uo pipefail

cmd=$(jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0

reason=""
if grep -qE 'LLM_PROVIDER=["'\'']?anthropic' <<<"$cmd"; then
  reason="실제 Claude API 호출(LLM_PROVIDER=anthropic)은 과금됩니다. 예상 비용을 안내했는지 확인하세요."
elif grep -qE '(^|[;&|][[:space:]]*|(bash|sh|zsh|source)[[:space:]]+)(\./)?deploy/[A-Za-z0-9_.-]+\.sh' <<<"$cmd"; then
  reason="deploy/ 스크립트는 클라우드 리소스를 만들거나 배포합니다. 예상 비용을 안내했는지 확인하세요."
fi

[ -z "$reason" ] && exit 0

jq -n --arg r "$reason" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: $r
  }
}'
