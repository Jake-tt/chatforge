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
- [ ] compose로 로컬 Langfuse를 띄우고 앱 트레이스 1건 확인(라우터·에이전트·도구 span)
- [ ] fake 모델 평가 실행이 데이터셋 실행으로 올라간다
- [ ] Mac 메모리 사용량을 기록하고, Ollama와 함께 돌릴 때의 주의를 README에 적는다

## Verification
- Package: G1·G2의 Manual 항목(참고)

## Blocked By
- 001, 002
