# 사용자 기억 그래프 (온톨로지 RAG) — Decision Interview

날짜: 2026-11-09 · 상태: 결정 확정, 검증 계획 확정, 조각 분할 검토 중

## 배경

사용자의 사실·선호·결정을 엔티티와 관계(그래프)로 저장해, 대화가 바뀌어도 기억하게 한다. 기존의
**지난 대화 검색**(`DEC-BACKEND-RAG-PAST-001`)은 "그때 무슨 말을 했나"(원문)를 찾고, 기억 그래프는
"이 사용자에 대해 무엇이 사실인가"(정리된 사실)를 담는다. 두 기능은 함께 있다.

## 확정한 결정

### D1. 반영 방식: 핵심 요약 + 조회 도구
- 자주 쓰이는 핵심 사실(최대 약 500자)을 매 턴 **현재 턴 context**에 붙인다. system 프롬프트는 바꾸지
  않아 프롬프트 캐시를 유지한다(`DEC-BACKEND-PROMPT-CACHE-001`).
- 그 밖의 관계는 agent가 `recall_memory` 도구로 그래프를 조회한다(엔티티 이름으로 찾아 1-hop 관계).

### D2. 추출 시점: 비활성 1시간 뒤 + 명시 요청
- 대화가 1시간 조용해지면 그 사이 메시지에서 한 번에 추출한다(비활성 압축 `DEC-BACKEND-SUMMARY-IDLE-001`과
  같은 예약 방식). 대화마다 어디까지 추출했는지 커서를 둔다.
- "기억해줘"는 agent의 `remember` 도구로 그 턴에 바로 저장하고, "잊어줘"는 `forget` 도구로 지운다.

### D3. 개인정보: 민감정보 제외 + 보기·삭제
- 자동 추출에서 건강, 금융 수치, 신분번호, 비밀번호, 정확한 위치, 정치·종교 성향을 제외한다.
  프롬프트로 제외하고, 저장 전에 코드 패턴(카드·주민번호·비밀번호 등)으로 한 번 더 거른다.
- 사용자가 직접 "기억해줘"한 내용은 저장한다(패턴 필터의 신분번호·비밀번호는 예외 없이 거부).
- 설정의 "기억" 화면에서 목록 보기, 항목 삭제, 전체 삭제, 기능 끄기를 제공한다.

### D4. 회원만
- 게스트는 기억을 쌓지 않는다(추출·주입·도구 모두 없음). 게스트→회원 이관 spec은 바꾸지 않는다.
- 가입 유도 문구에 "가입하면 기억해요"를 쓸 수 있다.

### D5. 대화 삭제 시 함께 삭제
- 기억마다 출처 대화를 기록하고, 대화를 지우면 그 대화에서만 나온 기억을 지운다.
  다른 대화에서도 확인된 기억은 남긴다.
- 탈퇴(`DEC-BACKEND-ACCOUNT-DELETION-001`)하면 기억 전체를 정리 작업에서 지운다.

### D6. 비용 (기존 규칙 적용)
- 자동 추출은 사용자가 요청한 작업이 아니므로 서비스가 부담하고 토큰을 로그에 남긴다
  (`.claude/rules/llm-agent.md` 예외 목록).
- `remember`·`recall_memory`·`forget` 도구 사용은 그 턴의 답변 토큰에 포함돼 평소대로 과금된다.

## 따를 기존 결정
- `DEC-BACKEND-RAG-PAST-001`, `DEC-BACKEND-PROMPT-CACHE-001`, `DEC-BACKEND-SUMMARY-IDLE-001`,
  `DEC-BACKEND-ACCOUNT-DELETION-001`, `DEC-BACKEND-AGENT-TOOLS-001`, `DEC-BACKEND-TASKS-001`,
  spec `guest-to-member-migration.md`

## Verification Plan

### 보통 위험: D1 반영 방식, D2 추출 시점, D4 회원 전용
Risk: medium
Selected Package: Standard Guardrail
Verification:
- Integration (HTTP API): 핵심 요약이 현재 턴 context에만 붙고 system은 그대로, `recall_memory`가 1-hop
  관계를 출처로 돌려줌, `remember`·`forget` 도구, 비활성 예약과 무효(새 메시지), 게스트에게는 도구·주입·
  추출이 없음
- Static/script: ruff, alembic check, decisions.py check
Blocking:
- PR blocking: 위 통합 테스트

### 높은 위험: D3 민감정보·보기·삭제, D5 삭제 경로, 탈퇴 정리, D6 비용
Risk: high
Selected Package: Strict Regression Net
Verification:
- Unit: 패턴 필터(카드·주민번호·비밀번호·계좌 형식)
- Integration (HTTP API): 목록·항목 삭제·전체 삭제·끄기(끄면 추출·주입·도구 모두 중지), 대화 삭제 시 그
  대화에서만 나온 기억 삭제·다른 출처가 있으면 유지, 탈퇴 정리에 기억 포함, 추출 토큰 미과금
- Fault injection: 추출 LLM 실패·타임아웃이면 아무것도 저장하지 않고 커서도 그대로(다음에 재시도)
- Static/script: Postgres 로컬 실행, 마이그레이션 upgrade/check/downgrade 왕복
- Manual: 로컬 Qwen에 민감정보가 섞인 표본 대화를 넣어 추출 결과 확인
Blocking:
- PR blocking: Unit, Integration, Fault injection
- Advisory: Manual(LLM 품질은 테스트로 완전히 증명할 수 없음)
Residual Risk:
- 민감정보 판별은 모델 품질에 달려 있다. 패턴 필터는 형식이 있는 값만 잡는다.
- 엔티티 이름 매칭은 문자열 기반이라 같은 대상을 다른 이름으로 부르면 따로 저장될 수 있다.

## Delivery Plan
Issue directory: `decision-docs/issues/261109-user-memory/`
| Slice | Type | Blocked By | Decisions | Verification |
|---|---|---|---|---|
| `001-storage-and-controls.md` | AFK | None | D3, D4, D5 | Strict Regression Net |
| `002-explicit-memory-and-recall.md` | AFK | 001 | D1, D3, D4 | Standard Guardrail |
| `003-automatic-extraction.md` | AFK | 002 | D2, D3, D6 | Strict Regression Net |
| `004-memory-screen.md` | HITL | 001 | D3 | Standard Guardrail + 화면 확인 |
