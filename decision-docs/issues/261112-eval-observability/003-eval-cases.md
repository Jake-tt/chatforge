# 평가 질문 세트 40개

Parent plan: `decision-docs/plans/261112-eval-observability-decision-interview.md`
Type: HITL

## Why This Type
라벨(기대 라우팅·기대 사실)이 곧 정답이다. Claude가 만든 라벨로 Claude가 채점하는 순환을 막으려면 사용자 검토가 필요하다(D3).

## Decisions Covered
D3

## What To Build
- 도움말 15, 계정 5, 지난 대화 5, 기억 5, 되물어야 하는 모호한 질문 5, 잡담·검색 불필요 5. 약 10개 멀티턴.
- 기대 사실은 `backend/knowledge/` 도움말 문서에서 가져온다.
- 검토하기 쉬운 표(마크다운)로 보여주고, 사용자 수정을 반영한 뒤 검토 표시를 켠다.

## Acceptance Criteria
- [ ] 40개 초안 커밋(검토 표시 꺼짐)
- [ ] 사용자 검토 반영, 검토 표시 켬
- [ ] 형식 검증 테스트 통과

## Verification
- Package: Standard Guardrail (G2)의 수동 항목
- Tests/checks: 형식 검증 테스트

## Blocked By
- 002
