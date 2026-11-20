# 로컬 Langfuse docker compose와 평가 실행 연동

Parent plan: `decision-docs/plans/261112-eval-observability-decision-interview.md`
Type: AFK

## Why This Type
D1(로컬 셀프호스팅)·D6(데이터셋 실행)이 정해져 있다. 버전 고정과 compose 위치는 계획을 바꾸지 않는 구현 세부다.

## Decisions Covered
D1, D6

## What To Build
- Langfuse 공식 docker compose를 기반으로 버전을 고정한 compose 파일과 실행·종료 안내(평가 때만 띄우기, 메모리 주의).
- 실행기가 Langfuse가 켜져 있으면 같은 실행을 데이터셋 실행(experiment)으로 올리고, 각 케이스를 트레이스와 연결한다.
- 운영 배포(`deploy/`)에는 아무것도 추가하지 않는다.

## Acceptance Criteria
- [x] compose로 로컬 Langfuse를 띄우고 앱 트레이스 1건 확인(라우터·에이전트·도구 span)
- [x] fake 모델 평가 실행이 데이터셋 실행으로 올라간다
- [x] Mac 메모리 사용량을 기록하고, Ollama와 함께 돌릴 때의 주의를 README에 적는다

## Result (2026-11-20)
- `dev/langfuse/docker-compose.yml`(Langfuse 4.76.0 고정, AI gateway 제외, UI·미디어만 127.0.0.1) + `dev/langfuse/up.sh`(비밀값·프로젝트 키 생성, headless 초기화).
- fake 3건, Ollama 2건(`--only past`)을 올려 ClickHouse에서 확인: 케이스마다 트레이스 1개에 `experiment-item-run` → `experiment-item-task`, 앱의 `chat_turn` → `router`·`agent`·`tools`·`ChatOllama`, 점수(`scores`)가 함께 있음.
- 구현 중 바뀐 것
  - Langfuse 서버 v4(events_only)는 `dataset_run_items.create`만으로는 실험을 만들지 않는다(요청은 성공하지만 실행이 생기지 않음). 실험은 SDK `run_experiment`가 span에 붙이는 속성으로 인식된다 → `run_experiment`를 작업 스레드에서 돌리고, 케이스는 앱이 있는 루프로 넘긴다.
  - FastAPI 0.162의 내장 OTel 계측이 요청 헤더로만 부모를 이어서, 프로세스 안 컨텍스트로는 앱 트레이스가 케이스 아래로 붙지 않았다 → 케이스 요청에 `traceparent`.
- 메모리: Langfuse 합계 약 3.0GB + `qwen3:8b` 6.2GB, 함께 돌리면 스왑 1.5GB → 6GB(README "로컬 Langfuse").

## Verification
- Package: G1·G2의 Manual 항목(참고)

## Blocked By
- 001, 002
