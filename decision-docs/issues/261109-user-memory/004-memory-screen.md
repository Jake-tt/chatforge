# 기억 화면
Parent plan: `decision-docs/plans/261109-user-memory-decision-interview.md`
Type: HITL

## Why This Type
화면 배치와 문구(무엇을 기억하는지 설명, 삭제 확인)를 사용자가 보고 정해야 한다.

## Decisions Covered
D3(보기·삭제·끄기)

## What To Build
- 설정에 "기억" 화면: 사실 목록, 항목 삭제, 전체 삭제(확인), 기능 끄기, 게스트에게는 가입 안내

## Acceptance Criteria
- [ ] 001의 API로 목록·삭제·끄기가 동작한다
- [ ] 사용자가 화면과 문구를 확인했다

## Verification
- Package: Standard Guardrail (프론트 typecheck·test·build) + 화면 확인

## Blocked By
- 001
