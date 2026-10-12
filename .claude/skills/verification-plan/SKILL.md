---
name: verification-plan
description: decision-interview로 확정한 결정마다 무엇으로 증명할지를 검증 패키지로 제안하고 합의해 같은 계획 파일에 기록한다. 구현 전에 검증 방식을 정해야 할 때 사용한다.
---

# Verification Plan

> 원작: 정준 · 수정: jake-tt (chatforge의 테스트 구조에 맞게 재작성)

각 결정을 어떻게 증명할지 정한다. 테스트 종류, 엄격도, 픽스처, 차단 여부, 남는 위험을 따로따로 묻지 않고 **패키지 하나로 묶어** 제안한다.

## 입력

`decision-interview`가 만든 계획 파일에서 시작한다.

```text
decision-docs/plans/YYMMDD-<topic>-decision-interview.md
```

경로가 없으면 경로나 주제를 묻는다. 사용자가 요청하지 않으면 별도 파일을 만들지 않고 같은 파일을 갱신한다.

## 절차

1. 계획 파일에서 확정한 결정, 남은 결정, `records.jsonl` 후보를 뽑는다.
2. 하나의 패키지로 함께 검증할 수 있는 결정은 묶는다.
3. 결정마다 위험도를 판단한다.
   - **Low**: 국소 동작, 좁은 단위, 쉬운 롤백
   - **Medium**: 사용자에게 보이는 동작, 모듈 경계, 스키마·API·SSE 이벤트 형식
   - **High**: 인증, 과금·쿼터, 데이터 손실, 마이그레이션, 보안, 되돌릴 수 없는 부수효과, 비싸거나 불안정한 검증(실제 LLM 호출)
4. 패키지 2~3개를 제안하고, 한 번에 하나씩 선택을 받는다(사용자가 "추천대로"라고 하면 일괄 적용).
5. 같은 계획 파일에 선택한 검증 계획을 기록한다.

## 패키지

원시 항목이 아니라 패키지를 제시한다. 정확히 하나를 추천으로 표시하고, 3개를 넘기지 않는다.

- **Minimal Smoke**: 가장 빠른 신호. 정상 경로 하나나 집중된 단위 테스트 하나.
- **Standard Guardrail** (기본 추천): 주요 동작과 핵심 엣지 케이스를 HTTP API 통합 테스트(`Harness`)로. PR 차단.
- **Strict Regression Net**: 고위험 결정용. 장애 주입(Redis 다운, LLM 타임아웃), 동시성, Postgres CI, 마이그레이션 왕복까지.

각 패키지에 추가·수정할 테스트, 차단 여부(PR 차단·참고·수동·보류), 비용과 위험을 한 문장으로 적는다.

## 질문 형식

```text
결정: <DEC id 또는 요약>
추천: Standard Guardrail — <이유>
선택지:
- A. Minimal Smoke — <검증 내용, 비용/위험>
- B. Standard Guardrail (추천) — <검증 내용, 비용/위험>
- C. Strict Regression Net — <검증 내용, 비용/위험>
질문: 이 결정에 B를 적용할까요?
```

허용하는 단축 답: "low/medium은 전부 추천대로", "high만 Strict", "이번 조각은 검증 보류". 일괄 적용해도 high 위험 결정은 따로 짚는다.

## 계획 파일 갱신

```markdown
## Verification Plan

### <결정 ID 또는 묶음 이름>

Risk: low | medium | high
Selected Package: <Minimal Smoke | Standard Guardrail | Strict Regression Net | Deferred>

Verification:
- Unit:
- Integration (HTTP API):
- Fault injection:
- Static/script: (ruff, alembic check, decisions.py check)
- Manual:

Blocking:
- PR blocking:
- Advisory:
- Deferred:

Residual Risk:
- ...
```

빈 항목은 뺀다.

## 경계

- 테스트를 직접 쓰지 않는다. `records.jsonl`도 요청 없이는 고치지 않는다. spec과 ADR도 만들지 않는다.
- 검증하기에 결정이 모호하면 decision-interview 방식으로 먼저 명확히 한다.
- 검증 비용이 구현 비용을 압도하면 그 사실을 알리고 더 작은 패키지를 추천한다.
