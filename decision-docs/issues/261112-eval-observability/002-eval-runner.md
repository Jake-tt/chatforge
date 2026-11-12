# 평가 실행기와 채점 규칙

Parent plan: `decision-docs/plans/261112-eval-observability-decision-interview.md`
Type: AFK

## Why This Type
지표 정의(감사 문서 A2 표), 판정 방식(D4), 결과 기록 위치(D6)가 정해져 있다. 질문 내용은 003에서 다루고, 이 조각은 형식과 실행·채점만 만든다.

## Decisions Covered
D2, D4, D6

## Existing Decisions To Follow
- 감사 문서 A2: CI에 넣지 않는다. 기본은 로컬 Ollama. Anthropic 실행은 비용 안내와 승인 후.
- `DEC-BACKEND-CITATION-CHECK-001`: 없는 번호 인용은 서버의 `answer cites missing sources`와 같은 기준으로 센다.
- 메모리 규칙: 외부 API는 상한과 캐시가 있는 스크립트로만 호출한다.

## What To Build
- `backend/evals/agent_cases.jsonl` 형식: id, 분류, 턴 목록(멀티턴), 준비할 상태(지난 대화·기억), 기대 라우팅 플래그, 기대 사실, 검토 여부. 형식 예시 몇 건만 넣는다(본 세트는 003).
- 실행기 `backend/evals/run_agent_eval.py`: 앱을 띄워 실제 HTTP 경로(가입→상태 준비→SSE 대화)로 케이스를 돌리고, 라우터 판단(구조화 로그)·이벤트·토큰·시간을 모은다.
- 채점: 라우팅 플래그별 정밀도·재현율, 되묻기 비율과 불필요한 되묻기, 기대 사실 포함, 없는 번호 인용 비율, 출처가 있는데 인용 0개 비율, 턴당 LLM 호출 수, billed_tokens, 첫 delta까지 시간, 전체 턴 시간. 로컬 LLM 판정은 '참고' 열로만.
- 결과: 케이스별 JSON(`backend/evals/results/`, gitignore)과 마크다운 요약표 출력.
- 보완 계획 문서(`plan/261028_hardening_plan.md`)의 PR E 범위 갱신(D2).

## Acceptance Criteria
- [ ] 채점 규칙 단위 테스트(각 지표의 경계 케이스 포함)
- [ ] 질문 세트 형식 검증 테스트(잘못된 라벨·빈 턴 거부)
- [ ] fake 모델로 실행기를 끝까지 도는 스모크 테스트가 요약표를 만든다
- [ ] 검토되지 않은 케이스가 섞이면 요약표에 '기준선 아님'으로 표시된다(D3)
- [ ] 보완 계획 문서 갱신

## Verification
- Package: Standard Guardrail (G2), Minimal Smoke (G3)
- Tests/checks: pytest(단위·스모크), ruff, decisions check

## Durable Artifacts
- `records.jsonl`: `DEC-EVAL-AGENT-001`
- README 실행 방법

## Blocked By
- None
