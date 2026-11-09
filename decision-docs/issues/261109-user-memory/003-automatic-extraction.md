# 비활성 후 자동 추출
Parent plan: `decision-docs/plans/261109-user-memory-decision-interview.md`
Type: AFK

## Why This Type
예약 방식(DEC-BACKEND-SUMMARY-IDLE-001)과 비용 규칙, 민감정보 2중 필터가 정해져 있다. 수동 확인은 로컬 모델로 비용 없이 한다.

## Decisions Covered
D2, D3(자동 추출 제외 범주), D6

## What To Build
- 답변마다 1시간 뒤 `extract_memory` 작업 예약(회원·기억 켜짐만), 대화별 추출 커서
- 구조화 출력으로 사실 추출 → 패턴 필터 → 엔티티 정규화·중복 병합 → 출처 기록
- 실패·타임아웃이면 무저장·커서 유지, 토큰은 로그만(미과금)

## Acceptance Criteria
- [ ] 새 메시지가 있으면 예약된 추출은 아무것도 하지 않는다
- [ ] 민감 범주와 패턴 값은 저장되지 않는다
- [ ] 같은 사실은 중복 저장되지 않고 출처만 더해진다
- [ ] 실패 시 커서가 그대로라 다음에 다시 시도한다
- [ ] 로컬 Qwen 표본 대화 수동 확인 결과를 PR에 남긴다

## Verification
- Package: Strict Regression Net

## Blocked By
- 002
