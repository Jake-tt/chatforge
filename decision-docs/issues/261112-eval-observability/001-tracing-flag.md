# Langfuse 트레이싱 플래그와 운영 차단

Parent plan: `decision-docs/plans/261112-eval-observability-decision-interview.md`
Type: AFK

## Why This Type
켜는 조건, 붙일 위치, 운영 차단 방식, 장애 시 동작이 D1·D5에 모두 정해져 있고, 검증 패키지(G1 Strict)도 확정됐다.

## Decisions Covered
D1, D5

## Existing Decisions To Follow
- `DEC-BACKEND-OBSERVABILITY-001`: request_id와 Cloud Logging은 그대로 둔다. 트레이스에 request_id를 연결한다.
- `DEC-BACKEND-AGENT-GRAPH-001`, `DEC-BACKEND-AGENT-STREAM-001`: SSE 계약과 스트림 처리를 바꾸지 않는다.
- 저장소 `CLAUDE.md`: 의존성은 `AppContext`로 받는다. 보조 호출은 `LLMProvider`를 거친다.

## What To Build
- `Settings.langfuse_enabled`(기본 false). 키·호스트는 Langfuse 표준 환경 변수(`LANGFUSE_*`)를 쓴다.
- 선택 의존성 `.[observability]`(langfuse). 꺼져 있으면 import하지 않는다.
- `K_SERVICE`가 있는 환경에서 켜져 있으면 설정 검증 오류로 서버 시작을 거부한다.
- 켜져 있으면 답변 그래프 실행과 보조 호출(라우팅·압축·추천 질문·제목·요약·기억 추출)에 `CallbackHandler`를 붙이고 request_id를 트레이스 메타데이터로 넘긴다.
- 종료 시 flush. 콜백 오류나 지연이 답변을 깨지 않게 한다.

## Acceptance Criteria
- [ ] 꺼져 있으면 콜백이 붙지 않고 langfuse를 import하지 않는다
- [ ] 켜면 답변 그래프와 보조 호출에 콜백이 붙고 request_id가 연결된다
- [ ] `K_SERVICE` 환경에서 켜면 설정 검증에서 거부된다
- [ ] 콜백이 예외를 던지거나 느려도 답변은 `done`으로 끝나고 저장·과금된다
- [ ] README 설계 포인트(관측)와 ARCHITECTURE 관측성 절 갱신

## Verification
- Package: Strict Regression Net (G1)
- Tests/checks: HTTP 통합 테스트(기록용 핸들러 스텁), 설정 단위 테스트, 장애 주입, ruff, decisions check

## Durable Artifacts
- `records.jsonl`: `DEC-BACKEND-TRACING-001`
- README·ARCHITECTURE

## Blocked By
- None
