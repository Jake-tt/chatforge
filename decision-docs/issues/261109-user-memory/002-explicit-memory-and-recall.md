# 명시 기억과 답변 반영
Parent plan: `decision-docs/plans/261109-user-memory-decision-interview.md`
Type: AFK

## Why This Type
도구 구조(DEC-BACKEND-AGENT-TOOLS-001)와 주입 위치(현재 턴 context)가 정해져 있다.

## Decisions Covered
D1(핵심 요약 + recall_memory), D3(직접 요청은 저장, 신분번호·비밀번호 패턴은 거부), D4

## Existing Decisions To Follow
DEC-BACKEND-AGENT-TOOLS-001, DEC-BACKEND-PROMPT-CACHE-001

## What To Build
- agent 도구 `remember`(주어·관계·대상), `forget`, `recall_memory`(이름으로 엔티티를 찾아 1-hop 관계를 출처로)
- 회원이고 기억이 켜져 있을 때만 도구를 제공하고, 핵심 사실 최대 약 500자를 현재 턴 context에 붙인다
- 저장 전 패턴 필터(카드·주민번호·비밀번호·계좌)

## Acceptance Criteria
- [x] "기억해줘"로 저장한 사실이 다음 대화의 현재 턴 context에 나타나고 system은 그대로다
- [x] recall_memory가 1-hop 관계를 출처로 돌려준다
- [x] 패턴에 걸리는 값은 저장하지 않고 모델에게 이유를 알린다
- [x] 게스트·기억 끔에서는 도구와 주입이 없다

## Verification
- Package: Standard Guardrail (+ 패턴 필터 단위 테스트)

## Blocked By
- 001
