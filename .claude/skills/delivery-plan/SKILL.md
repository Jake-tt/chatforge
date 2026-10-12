---
name: delivery-plan
description: 결정과 검증 계획이 정리된 계획 파일을 독립적으로 가져갈 수 있는 작업 조각으로 나누고, 각 조각을 에이전트 단독 진행(AFK) 또는 사람 개입 필요(HITL)로 분류해 decision-docs/issues에 파일로 만든다. decision-interview와 verification-plan 다음에 사용한다.
---

# Delivery Plan

> 원작: 정준 · 수정: jake-tt (chatforge의 경로와 슬라이싱 규칙에 맞게 재작성)

확정된 결정과 검증 패키지를 구현 가능한 조각으로 바꾼다. 핵심은 각 조각을 독립적으로 가져갈 수 있게 만들고, 에이전트가 사람 없이 끝낼 수 있는지(AFK) 중간에 사람의 판단이 필요한지(HITL)를 분명히 표시하는 것이다.

## 입력

```text
decision-docs/plans/YYMMDD-<topic>-decision-interview.md
```

계획 파일에 결정과 `Verification Plan`이 있어야 한다. 검증 계획이 없으면 명백히 안전한 조각만 계획하고, 불확실한 조각은 HITL로 표시하거나 `verification-plan`으로 돌려보낸다.

## AFK와 HITL

결정과 검증 기준이 이미 정해졌다면 AFK를 우선한다.

**AFK**: 기존 결정·spec·ADR·검증 계획만으로 구현할 수 있고, 중간에 제품·설계·아키텍처·데이터·보안·배포 판단이 필요 없고, 완료 기준이 객관적이며 필요한 테스트가 명확하다.

**HITL**: 사람이 동작, 문구, 화면, 배포 정책, 데이터 마이그레이션 전략, 보안 수준, 아키텍처를 구현 중에 골라야 한다. 기존 결정과 충돌할 수 있다. 검증 전략이 정해지지 않았다. 외부 자격증명, 운영 데이터, 실제 LLM 호출(비용), 수동 QA 승인이 필요하다.

복잡하다는 이유만으로 HITL로 표시하지 않는다. **실행 중에 결정이 필요할 때만** HITL이다.

## 절차

1. 계획 파일에서 결정, 검증 계획, 남은 질문, 제약을 뽑는다.
2. Vertical 조각으로 초안을 만든다. 각 조각은 좁고, 완결되고, 독립적으로 검증할 수 있어야 한다(CLAUDE.md "작업 슬라이싱").
3. 남은 선택지를 결정이나 검증 패키지로 되돌려 AFK로 만들 수 있으면 그렇게 한다.
4. 사용자가 바로 만들라고 하지 않았다면, 파일을 쓰기 전에 조각 목록을 보여주고 승인을 받는다.
5. 승인된 조각을 `decision-docs/issues/<plan-slug>/001-short-title.md` 형식으로 쓴다. 선행 조각을 먼저 만든다.
6. 계획 파일에 `## Delivery Plan` 요약을 추가하고 이슈 파일을 연결한다.

"백엔드만", "UI만" 같은 수평 조각은 그 자체로 가치 있고 테스트할 수 있을 때만 쓴다. 넓은 조각 하나보다 얇은 조각 여러 개를 선호한다.

## 검토 요청

조각마다 제목, AFK/HITL과 그 이유, 선행 조각, 다루는 결정, 검증 패키지, 완료 기준 요약을 보여주고 묻는다.

```text
조각 분할과 AFK/HITL 분류가 맞나요?
```

## 이슈 파일 템플릿

```markdown
# <조각 제목>

Parent plan: `decision-docs/plans/YYMMDD-<topic>-decision-interview.md`
Type: AFK | HITL

## Why This Type
## Decisions Covered
## Existing Decisions To Follow
## What To Build
## Acceptance Criteria
- [ ] ...
## Verification
- Package:
- Tests/checks:
## Durable Artifacts
- `records.jsonl`, specs/adrs, README 설계 포인트
## Blocked By
- None
```

## 계획 파일 갱신

```markdown
## Delivery Plan

Issue directory: `decision-docs/issues/<plan-slug>/`

| Slice | Type | Blocked By | Decisions | Verification |
|---|---|---|---|---|
| `001-short-title.md` | AFK | None | DEC-... | Standard Guardrail |
```

## 경계

- 코드를 구현하지 않는다. 요청 없이는 GitHub 이슈를 만들지 않고 파일로만 만든다.
- 계획 중에 결정을 바꾸지 않는다. 빠진 결정이 보이면 멈추고 `decision-interview`로 돌려보낸다.
- 검증을 지어내지 않는다. 없으면 HITL로 표시하거나 `verification-plan`으로 돌려보낸다.
