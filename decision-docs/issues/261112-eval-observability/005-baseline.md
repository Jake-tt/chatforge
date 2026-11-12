# 기준선 실행과 표본 수기 확인

Parent plan: `decision-docs/plans/261112-eval-observability-decision-interview.md`
Type: HITL

## Why This Type
표본 10건의 근거 충실도는 사람이 확인한다(D4). 수치 해석(A3·A5·A7 판단)도 사용자 결정이다.

## Decisions Covered
D4, D6

## What To Build
- 검토된 세트로 로컬 Ollama(qwen3:8b) 기준선 실행. 비용 없음.
- 요약표를 `plan/` 문서에 커밋하고, 감사 문서 A2를 닫는다.
- 표본 10건(분류별 고르게)을 사용자에게 보여주고 판정을 기록한다.

## Acceptance Criteria
- [ ] 기준선 요약표 커밋(모델·커밋·날짜 명시)
- [ ] 표본 10건 사용자 판정 기록
- [ ] 감사 문서 A2 행 갱신, A3·A5·A7의 다음 판단 메모

## Verification
- Package: G2 Manual

## Blocked By
- 002, 003
