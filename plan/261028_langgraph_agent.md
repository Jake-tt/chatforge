# LangGraph 에이전트 전환 계획

## 목적

chatforge의 대화 흐름이 고정 파이프라인(라우터 → 검색 → 답변)이라 실무에서 설계한 에이전트 구조가 드러나지 않는다. 실무에서 설계한 LangGraph 에이전트 구조를 **설계만 가져와 새로 구현**한다. 회사 코드는 복사하지 않는다.

## 실무 설계에서 가져오는 것

| 실무 설계 | chatforge 구현 |
|---|---|
| `StateGraph` 진입점 = router 노드 | 동일. 구조화 출력으로 의도·검색 쿼리·필요한 소스 판단 |
| router 타임아웃 시 의도 재확인으로 fallback | 동일. 타임아웃·오류 시 `clarify` 경로 |
| master_router(조건부 엣지): 부적절 사용 / 대상 불명확 / 병렬 조회 / 바로 답변 | `clarify` / `retrieve` / `answer` 세 갈래 |
| parallel_coordinator: 검색 계층 + 개인 기록 prefetch를 `asyncio.gather` | 동일. 도움말 검색 + 지난 대화 + 계정 현황 (기존 `retrieval.gather_sources` 재사용) |
| main = ReAct 에이전트 (도구 호출) | 동일. 도구: `search_help`, `search_past_conversations`, `get_account_usage` (나중에 `search_pubmed`) |
| MALFORMED 재시도 시 도구 없이 LLM 직접 호출 | 도구 호출 형식 오류 시 도구를 뺀 모델로 1회 재시도, 도구 반복 상한 |
| 요약 에이전트는 의도 재확인 턴에서 실행 금지 | 기존 요약 경계 규칙 + `clarify` 턴에서 요약 생략 |
| 추천 질문 에이전트 분리 | 기존 `suggestions` 재사용 |
| LangSmith 트레이싱 | 환경변수로 선택(기본 끔) |

## 모델 계층

- LangChain 채팅 모델로 통일: `ChatOllama`(Qwen3 8B, 로컬·무과금, 도구 호출 지원), `ChatAnthropic`(프롬프트 캐싱 유지), 테스트용 fake 채팅 모델
- 과금은 LangChain `usage_metadata`에서 입력·출력·캐시 토큰을 읽어 기존 `Usage.billed()`로 계산

## 단계별 PR (추가 → 전환 → 제거)

1. **PR A — 모델 계층 추가** (완료, #4): LangChain 채팅 모델 팩토리(anthropic / ollama / fake)와 `usage_metadata` → `Usage` 변환. 기존 경로는 그대로
2. **PR B — 에이전트 그래프 추가** (완료, #5): `backend/app/agent/`에 상태·노드·그래프. `AGENT_ENABLED` 플래그(기본 끔). HTTP 테스트에서 플래그를 켜고 fake 모델로 도구 호출 시나리오 검증
3. **PR C — 호출부 전환** (리뷰 완료, 2026-10-28): `send_message`가 에이전트 그래프를 쓰도록 전환. SSE 이벤트 계약(`delta`·`sources`·`suggestions`·`done`)과 과금은 그대로 유지
4. **PR D — 기존 경로 제거**: 고정 파이프라인 오케스트레이션(`RagPipeline.ground` 등)과 쓰이지 않게 된 코드 제거
   - `_stream_reply`, `RagPipeline.ground`·`route`, `LLMProvider.stream`과 `AnthropicProvider.stream`(캐싱 테스트는 `to_messages(cache=True)`·ChatAnthropic 기준으로 이전), `FakeProvider.stream`, `AGENT_ENABLED` 플래그
   - `_build_rag`가 항상 `RagPipeline`을 돌려주게 좁혀 `rag is None`과 `rag.search is None`의 이중 "없음"을 하나로
   - `test_the_legacy_pipeline_still_answers_when_the_agent_is_off` 제거
5. **PR E — PubMed 도구와 평가 파이프라인**: 데이터 확보 확인 후 별도 진행

## 위험 (Risk-First)

- **로컬 모델**: Gemma는 Ollama에서 네이티브 도구 호출 지원이 확인되지 않아 Qwen3 8B로 결정(2026-10-28). Qwen3의 thinking 출력은 라우터 등 보조 호출에서 끈다
- **스트리밍**: 그래프 실행 중 main 노드의 토큰만 `delta`로 내보내야 한다(`stream_mode="messages"`)
- **프롬프트 캐싱**: LangChain 경유 시에도 system 고정·breakpoint 위치가 유지되는지 기존 테스트로 확인

## 검증

- 각 PR: `ruff`, `pytest`(SQLite·Postgres), 결정 기록 갱신
- 에이전트 동작은 fake 모델로 HTTP 테스트: 경로 분기, 라우터 타임아웃 fallback, 도구 호출, 도구 형식 오류 재시도, 반복 상한
- 실제 품질과 지연은 사용자가 M4 Mac mini에서 Qwen3 8B로 확인
