# 에이전트 품질 평가(감사 A2)와 Langfuse 관측 — 결정 인터뷰

## Context
- 감사 A2(`plan/261106_agent_audit.md`): 테스트는 전부 fake 모델로 오케스트레이션 계약만 검증하고, 실제 모델의
  라우팅 정확도·되묻기 비율·인용 정합률·근거 충실도·비용·지연을 모른다. A3·A5·A7은 이 수치를 보고 정한다.
- 사용자 요청(2026-11-12): Langfuse로 관측을 도입한다.
- 확인한 사실(Langfuse 문서, context7):
  - Python SDK v4는 OpenTelemetry 기반이다. LangChain·LangGraph는 `CallbackHandler`로 붙는다.
  - 전송 전 마스킹은 `mask_otel_spans`로 한다. `flush`는 네트워크 오류에도 예외를 던지지 않는다.
  - 셀프호스팅 최소 구성: Web·Worker(각 2CPU/4GiB), Postgres(2CPU/4GiB), Redis(1CPU/1.5GiB),
    ClickHouse(2CPU/8GiB), S3 호환 저장소. v4 서버는 ClickHouse 25.12 이상을 요구한다.

## 검토한 기존 결정
- `DEC-BACKEND-OBSERVABILITY-001`: 운영 관측은 Cloud Logging JSON과 request_id이고, 라우터 판단은 구조화
  로그로 남긴다. 유지한다.
- `plan/261028_hardening_plan.md` §2-6, §3: 에이전트 정량 평가는 PR E(PubMed + 평가 파이프라인)에 포함하기로
  했다. A2를 따로 진행할지는 D2에서 정한다.
- 저장소 `CLAUDE.md`: 클라우드 리소스와 `LLM_PROVIDER=anthropic`은 비용 승인이 필요하다. 로컬 Ollama는 무료다.

## 확정한 결정

### D1. Langfuse는 로컬 Docker 셀프호스팅, 개발·평가 전용
- docker compose로 사용자의 Mac에서만 돌린다. 운영(Cloud Run)에는 보내지 않고, 운영 관측은
  `DEC-BACKEND-OBSERVABILITY-001` 그대로 유지한다.
- 이유: 데이터가 밖으로 나가지 않고 비용이 없다.
- 감수할 것: 16GB Mac에서 Ollama(qwen3:8b)와 메모리를 다툰다. 평가를 돌릴 때만 띄우는 운영 방식이 필요하다.

### D2. A2는 PR E(PubMed)와 분리해 먼저 진행
- 지금 있는 경로(도움말·계정·지난 대화·기억·되묻기·잡담)로 라우팅·인용·비용·지연 기준선을 먼저 만든다.
  그래야 A3·A5·A7을 바로 판단할 수 있다.
- PubMed 검색 평가(recall@k, MRR 등)는 PR E에 남기고, 이번에 만드는 평가 틀을 재사용한다.
- 바뀌는 기존 결정: `plan/261028_hardening_plan.md` §2-6("PR E에 포함")과 §3 PR E 행. 같은 작업에서 문서를 고친다.

### D3. 질문 세트는 Claude가 초안을 만들고 사용자가 검토해 확정(HITL)
- 40개: 도움말 15, 계정 5, 지난 대화 5, 기억 5, 되물어야 하는 모호한 질문 5, 잡담·검색 불필요 5.
  이 중 약 10개는 멀티턴.
- 라벨: 기대 라우팅(`needs_search`·`needs_account`·`needs_past`·`needs_clarification`), 답에 들어야 할
  사실(도움말 문서 기준).
- 지난 대화·기억이 필요한 케이스는 실행 전에 HTTP API로 상황을 준비한다(실제 경로를 탄다).
- 자기 라벨로 자기를 채점하는 순환을 막기 위해, 사용자 검토 전에는 기준선으로 쓰지 않는다.

### D4. 근거 충실도는 규칙과 표본 수기 확인, LLM 판정은 참고
- 기본 지표(규칙): 기대 사실 포함 여부, 없는 번호 인용 비율, 출처가 있는데 인용 0개인 비율.
- 표본 10건은 사람이 직접 확인한다.
- 로컬 qwen3:8b의 LLM 판정은 같은 모델의 자기 채점 편향이 있으므로 "참고"로만 표시한다.
- Claude를 판정 모델로 쓰는 것은 비용 승인 후 별도로 한다.

### D5. 트레이싱은 설정 플래그(기본 끔), 운영에서는 켜지지 않게 코드로 막음
- `LANGFUSE_ENABLED`(기본 false)와 선택 의존성 `.[observability]`.
- 켜면 답변 그래프와 보조 호출(라우팅·압축·추천 질문·제목·요약·기억 추출)에 `CallbackHandler`를 붙이고,
  request_id를 트레이스에 연결한다.
- Cloud Run(`K_SERVICE` 환경 변수가 있음)에서 켜져 있으면 서버 시작 단계에서 설정 오류로 거부한다(D1을 코드로 강제).
- Langfuse 전송 실패는 답변에 영향을 주지 않는다(SDK `flush`는 예외를 던지지 않음. 테스트로 확인).

### D6. 결과는 요약표는 저장소에, 케이스별 상세는 Langfuse에
- 평가 실행기는 Langfuse 없이도 돈다. 케이스별 결과는 JSON으로 남기고 커밋하지 않는다(`.gitignore`).
- 지표 요약표는 `plan/` 문서에 커밋해 변경 전후 비교에 쓴다.
- Langfuse가 켜져 있으면 같은 실행을 데이터셋 실행(experiment)으로 올려 케이스별 트레이스를 볼 수 있게 한다.

## 남은 질문 (계획을 바꾸지 않아 구현 중 정함)
- docker compose 파일의 위치와 Langfuse 버전 고정 값. Ollama와 메모리를 다투므로, 평가 때만 띄우는 방법을 README에 적는다.
- 측정 지표의 세부 정의(첫 delta 시간 측정 지점 등). 감사 문서 A2 표를 따른다.

## Verification Plan

### G1. 트레이싱 플래그와 운영 차단 (D1, D5)

Risk: high (실수로 켜지면 사용자 대화가 트레이스로 나가는 개인정보 경계)
Selected Package: Strict Regression Net (DB 변경이 없어 마이그레이션·Postgres 항목 제외)

Verification:
- Integration (HTTP API):
  - 기본값(꺼짐)이면 답변 그래프와 보조 호출에 콜백이 붙지 않는다
  - 켜면 답변 그래프와 보조 호출에 콜백이 붙고 트레이스에 request_id가 연결된다(Langfuse 핸들러는 기록용 스텁)
- Unit: `K_SERVICE`가 있는 환경에서 `LANGFUSE_ENABLED=true`면 설정 검증에서 거부된다
- Fault injection: 콜백이 예외를 던지거나 느려도 답변은 `done`으로 끝나고 저장·과금된다
- Static/script: ruff, `decisions.py check --strict-paths --strict-names`
- Manual: 로컬 Langfuse에 실제 트레이스 1건이 보이는지(라우터·에이전트·도구 span)

Blocking:
- PR blocking: Integration, Unit, Fault injection
- Advisory: Manual

Residual Risk:
- 운영 판별을 `K_SERVICE`에 기대므로, Cloud Run이 아닌 다른 운영 환경에서는 막지 못한다.

### G2. 평가 실행기, 채점, 결과 기록 (D3, D4, D6)

Risk: medium (채점 규칙이 틀리면 기준선 전체가 틀린다)
Selected Package: Standard Guardrail + 수동 검토

Verification:
- Unit: 채점 규칙(라우팅 플래그별 정밀도·재현율, 되묻기 비율, 기대 사실 포함, 없는 번호 인용 비율, 출처가 있는데
  인용 0개 비율), 질문 세트 JSONL 형식 검증
- Integration: fake 모델로 실행기를 끝까지 한 번 돌리는 스모크(실제 HTTP 경로, 지난 대화·기억 준비 포함).
  요약표가 만들어지는지 확인
- Manual:
  - 질문 세트 40개 사용자 검토(HITL)
  - 로컬 Ollama 기준선 실행과 표본 10건 수기 확인
  - Langfuse가 켜져 있을 때 데이터셋 실행이 올라가는지

Blocking:
- PR blocking: Unit, Integration
- Advisory: Manual (실제 모델은 비결정적이고 CI에 없음)

Residual Risk:
- LLM 품질 지표는 표본 40개 규모라 오차가 크다. 전후 비교는 같은 세트·같은 모델로만 한다.

### G3. A2를 PR E에서 분리 (D2)

Risk: low
Selected Package: Minimal Smoke

Verification:
- Static/script: 보완 계획 문서 갱신, `decisions.py check`

## 작업 계획 인계 (조각 후보)
1. 트레이싱 플래그와 운영 차단(AFK)
2. 평가 실행기와 채점 규칙, 질문 세트 형식(AFK)
3. 질문 세트 40개 초안 → 사용자 검토(HITL)
4. 로컬 Langfuse docker compose와 실행 안내(AFK, 사용자 Mac에서 확인)
5. 기준선 실행과 요약표 커밋, 표본 10건 수기 확인(HITL)
6. 보완 계획 문서(PR E 범위) 갱신

## records.jsonl·spec·ADR 후보
- 새 결정 후보: `DEC-BACKEND-TRACING-001`(D5), `DEC-EVAL-AGENT-001`(D3·D4·D6)
- ADR 후보: 관측 도구 선택(Langfuse 로컬 셀프호스팅, 운영 제외, D1). 사용자가 요청하면 만든다.

## Delivery Plan

Issue directory: `decision-docs/issues/261112-eval-observability/`

| Slice | Type | Blocked By | Decisions | Verification |
|---|---|---|---|---|
| `001-tracing-flag.md` | AFK | None | D1, D5 | Strict Regression Net (G1) |
| `002-eval-runner.md` | AFK | None | D2, D4, D6 | Standard Guardrail (G2), Minimal (G3) |
| `003-eval-cases.md` | HITL | 002 | D3 | G2 수동 |
| `004-local-langfuse.md` | AFK | 001, 002 | D1, D6 | G1·G2 수동(참고) |
| `005-baseline.md` | HITL | 002, 003 | D4, D6 | G2 수동 |
