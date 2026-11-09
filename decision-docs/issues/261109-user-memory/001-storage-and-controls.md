# 기억 저장소와 통제 API
Parent plan: `decision-docs/plans/261109-user-memory-decision-interview.md`
Type: AFK

## Why This Type
D3·D4·D5와 Strict 검증 기준이 모두 정해져 있어, 구현 중 사람의 판단이 필요 없다.

## Decisions Covered
D3(보기·삭제·끄기), D4(회원만), D5(대화 삭제·탈퇴 시 정리)

## Existing Decisions To Follow
DEC-BACKEND-ACCOUNT-DELETION-001, DEC-BACKEND-OWNERSHIP-001, spec guest-to-member-migration.md

## What To Build
- 마이그레이션: `memory_entities`(사용자별 정규화 이름 유일), `memory_facts`(주어 엔티티·관계·대상 엔티티 또는 값), `memory_fact_sources`(사실별 출처 대화), `users.memory_enabled`
- `GET /memory`(켜짐 여부와 사실 목록), `DELETE /memory/{id}`, `DELETE /memory`, `PUT /memory/settings`
- 게스트는 403 `members_only`
- 대화 삭제(단건·일괄) 시 그 대화의 출처를 지우고, 출처가 남지 않은 사실을 지운다
- 탈퇴 정리 작업이 기억 전체를 지운다

## Acceptance Criteria
- [x] 회원은 자기 기억만 보고 지운다(남의 사실 ID는 404)
- [x] 끄면 `enabled=false`가 저장되고 기존 사실은 남는다
- [x] 대화를 지우면 그 대화에서만 나온 사실이 지워지고, 다른 출처가 있는 사실은 남는다
- [x] 탈퇴 정리 후 기억 행이 남지 않는다
- [x] 마이그레이션 upgrade/check/downgrade 왕복, Postgres 실행

## Verification
- Package: Strict Regression Net
- Tests/checks: HTTP 통합 테스트, Postgres 로컬, alembic 왕복, decisions.py check

## Durable Artifacts
- records.jsonl 새 결정, README 설계 포인트, ARCHITECTURE 데이터 모델·API

## Blocked By
- None
