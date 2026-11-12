# 남은 작업 정리 (2026-11-12 기준)

다른 세션에서 이어서 작업할 수 있도록, 지금까지 끝난 것과 남은 것, 시작 방법을 한곳에 모은 문서입니다.
작업을 하나 끝내면 이 문서의 해당 행을 갱신합니다.

## 0. 현재 상태

- `main`: `7ff5149`(PR #37 머지)까지 반영. 열린 PR 없음
- 마이그레이션 head: `0013_user_memory` (`0010` → `0012` → `0013` 순서. `0011`은 없음). 기억 003 PR이 `0014_memory_extraction_cursor`를 추가
- 백엔드 테스트 343개, 프론트 테스트 23개 통과. 결정 기록 51건(`decision-docs/records.jsonl`)
- 최근 완료된 큰 작업
  - 첨부: 문서→마크다운(#30), 이미지(비전 모델 전용, #31), 스캔 PDF OCR(#32), 잘린 문서 검색 도구(#33), 파싱 자식 프로세스 격리(#34), 업로드 즉시 응답 + 백그라운드 변환(#36)
  - 사용자 기억 그래프: 저장소·통제 API(#35), 답변 반영(핵심 요약 + `remember`·`recall_memory`·`forget`, #37)
  - 그 외: recap(#29), 표·도식 답변(#28), Tavily 웹 검색(#24), Ollama 콜드 스타트(#23), 비활성 압축(#22), 되묻기 선택지(#20)

## 1. 남은 작업 (우선순위 순)

| # | 작업 | 유형 | 준비 상태 | 근거 문서 |
|---|---|---|---|---|
| 1 | 기억 그래프 003: 비활성 1시간 뒤 자동 추출 | AFK | PR 올림(`feat/memory-extraction`), 머지 대기 | `decision-docs/issues/261109-user-memory/003-automatic-extraction.md` |
| 2 | 에이전트 감사 A1: 추천 질문 생성 중 연결이 끊기면 답변 미저장·미과금 (P0) | 버그 | 원인·완료 기준 정리됨 | `plan/261106_agent_audit.md` A1 |
| 3 | 기억 그래프 004: 기억 화면 | HITL | 화면·문구를 사용자가 확인해야 함 | `decision-docs/issues/261109-user-memory/004-memory-screen.md` |
| 4 | 에이전트 감사 A2~A9 | 혼합 | 감사 문서의 PR 순서(§3)를 따름 | `plan/261106_agent_audit.md` |
| 5 | PR E: PubMed 도구와 평가 파이프라인 | 진행 중 | PMC 수집 스크립트(`backend/evals/fetch_pmc.py`)까지만 머지 | `plan/261028_langgraph_agent.md` PR E, `plan/261028_hardening_plan.md` |
| 6 | 첨부 후속 (아래 §2) | 혼합 | 항목별 | 이 문서 §2 |
| 7 | 에이전트·모델 후속 (아래 §3) | 혼합 | 항목별 | 이 문서 §3 |
| 8 | 브라우저 수동 확인 (아래 §4) | 수동 | 사람이 화면을 봐야 함 | 이 문서 §4 |

### 1-1. 기억 그래프 003에서 알아둔 것

- 쓰기 경로는 `memory.add_fact` 하나뿐입니다. 추출 결과도 이것으로 저장합니다(중복 사실은 출처만 더해짐).
- 예약 방식은 비활성 압축(`summary.compact_idle_conversation`, `DEC-BACKEND-SUMMARY-IDLE-001`)과 같게 합니다. 답변마다 1시간 뒤 작업을 예약하고, 그때 새 메시지가 있으면 아무것도 하지 않습니다.
- 회원이고 `users.memory_enabled`일 때만 추출합니다. 게스트는 기억이 없습니다(`DEC-BACKEND-MEMORY-STORE-001`).
- 민감정보는 2중으로 막습니다. 추출 프롬프트에서 건강·금융 수치·신분번호·비밀번호·정확한 위치·정치·종교를 제외하고, 저장 전에 `memory.sensitive_reason`으로 한 번 더 거릅니다.
- 추출 토큰은 사용자에게 과금하지 않고 로그로 남깁니다(`.claude/rules/llm-agent.md`의 예외).
- 실패·타임아웃이면 아무것도 저장하지 않고 커서도 그대로 둡니다(다음에 재시도). 대화별 커서 컬럼은 마이그레이션이 필요합니다(`0014`).
- 검증은 Strict Regression Net입니다. 로컬 Qwen으로 민감정보가 섞인 표본 대화를 넣어 추출 결과를 PR에 남깁니다.
- 002에서 실제 모델로 찾은 함정(같은 실수 반복 금지)
  - 라우터는 사용자 입력만 봅니다. 라우터가 알아야 하는 것은 `router_prompt`에 넣어야 합니다(기억은 넣어 둠).
  - 로컬 모델은 context 블록을 번호 출처로 착각해 `[1]`로 인용합니다. 블록에 "번호 출처가 아님"을 밝힙니다.
  - 사용자 자신을 가리키는 "나"는 모델에게 "사용자"로 보여줍니다(`memory._SELF`).
- 003에서 실제 모델로 찾은 함정
  - 로컬 8B는 프롬프트의 "추출하지 마라"(건강·정치·종교·주소)를 무시하지만, 사실마다 `category`를 고르게 하면 분류는 맞게 합니다. 그래서 코드가 민감 범주를 버립니다.
  - "판교에서 일한다"를 "사는 곳: 판교"로 적는 등 관계 이름이 부정확할 때가 있습니다. 004 기억 화면에서 사용자가 지울 수 있습니다.

## 2. 첨부 후속

| 항목 | 내용 | 비고 |
|---|---|---|
| 오래된 `processing` 첨부 정리 | 변환 작업이 유실되면 행이 `processing`과 `pending_data`(최대 10MB)를 가진 채 남음 | 일정 시간이 지난 행을 `failed`로 바꾸고 바이트를 비우는 정리 작업 |
| 쓰지 않는 원본 정리 | 로컬 디스크(`ATTACHMENTS_DIR`)의 원본은 참조가 끊겨도 지워지지 않음 | 대화 삭제·탈퇴 시 참조가 없는 해시 삭제 |
| 업로드 전 쿼터 검사 | 스캔 PDF는 비전 호출이 최대 10번이지만 과금은 변환 뒤 | 남은 쿼터가 부족하면 업로드 단계에서 거절 |
| 글자·스캔 혼합 PDF | 지금은 전체 글자가 20자 미만일 때만 OCR | 페이지별 판별 |
| 이미지 미리보기 | 말풍선에 파일 이름만 표시 | 원본을 인증해서 내려주는 API 필요 |
| 첨부 검색 색인 캐싱 | `search_attachments`가 매 호출마다 문단 분할·색인 | 첨부가 많아지면 필요 |
| 실제 Claude 확인 | 이미지·스캔 OCR은 fake·로컬로만 확인함 | 비용 발생: 승인 후 `LLM_PROVIDER=anthropic`으로 이미지 1장, 스캔 PDF 1건 |

## 3. 에이전트·모델 후속

| 항목 | 내용 | 비고 |
|---|---|---|
| 도구 메시지 저장과 `tool_call_id` 관리 | 도구 호출·결과를 DB에 저장해 다음 턴에 다시 보내는 것 | `DEC-BACKEND-PROMPT-CACHE-001`(출처는 저장하지 않음)을 뒤집는 결정이라 decision-interview부터. `messages.kind`를 같은 축으로 확장하고 `role="tool"`·`tool_call_id` 추가, 요약 경계가 tool_use와 tool_result 사이를 자르지 않게 |
| 간헐적 라우터 타임아웃 | 콜드 스타트 수정(#23) 뒤에도 로컬에서 가끔 `router timed out` | 원인 측정부터. 실패해도 agent가 도구를 직접 고르는 경로로 답은 나감 |
| 출처 없는 `[n]` 표기 | 로컬 8B는 출처가 없어도 가끔 `[1]`을 붙임 | 서버에서 지우려면 인용 검사 결정(`DEC-BACKEND-RAG-CITATIONS-001`) 변경 |
| OpenAI(GPT) provider | 비전은 지금 Claude에서만(`Settings.supports_vision`) | provider를 추가하면 능력값으로 자동으로 켜짐 |

## 4. 브라우저 수동 확인 (아직 안 함)

자동 테스트·타입 검사·빌드는 통과했지만, 아래 화면은 브라우저로 직접 보지 못했습니다. `cd frontend && npm run dev`(백엔드는 별도로 실행)로 확인합니다.

- 표·mermaid 도식 렌더링, 다크 모드(#28)
- recap 카드(#29). 확인하려면 백엔드를 `RECAP_AFTER_SECONDS=0`으로 실행
- 첨부 버튼·칩·"읽는 중" 표시·실패 안내(#30, #31, #36)

## 5. 하지 않기로 한 것 (다시 꺼내기 전에 이유 확인)

| 항목 | 결정 | 이유 |
|---|---|---|
| 운영 원본 저장소 GCS | 제외 | 비용. 로컬 디스크와 DB 임시 저장으로 운영 |
| 음성 입력 | 보류 | 급하지 않음. 한다면 브라우저 Web Speech API부터 |
| 영상 첨부 | 보류 | 비용과 처리 시간이 가장 큼 |
| 로컬 비전 모델(qwen3-vl:8b) | 채택 안 함 | 16GB Mac에서 thinking을 끌 수 없고 스왑이 심해, 라우팅 지연 중앙값 242초(qwen3:8b 3.6초). 멀티모달은 Claude/GPT 같은 비전 provider에서만 |
| 게스트 기억 | 하지 않음 | 기억은 회원만(가입 유도 포인트) |

## 6. 작업 환경 메모

- 파이썬: `backend/.venv`는 Python 3.12(CI·Dockerfile과 같음). 의존성은 `uv pip install --python .venv/bin/python -e ".[dev]"`로 설치합니다. `markitdown`은 3.15를 지원하지 않습니다.
- 로컬 LLM: `ollama serve` 후 `LLM_PROVIDER=ollama`(qwen3:8b). 서버가 시작할 때 모델을 미리 올립니다(`OLLAMA_KEEP_ALIVE=-1`). Ollama 비용은 없습니다.
- `LLM_PROVIDER=anthropic`, 클라우드 리소스, `deploy/*.sh`는 비용이 듭니다. 예상 비용을 먼저 알리고 승인을 받습니다(저장소 `CLAUDE.md`).
- Postgres 확인: SQLite는 외래 키를 검사하지 않아, 실제로 Postgres에서만 드러난 버그가 있었습니다(#30 flush 순서, #25 drain). 마이그레이션이나 삭제 경로를 바꾸면 일회용 Postgres로도 돌립니다.

  ```bash
  initdb -D /tmp/pg -U chatforge --auth=trust
  pg_ctl -D /tmp/pg -o "-p 55432 -k ''" -l /tmp/pg.log start
  createdb -h 127.0.0.1 -p 55432 -U chatforge chatforge
  TEST_DATABASE_URL=postgresql+asyncpg://chatforge@127.0.0.1:55432/chatforge .venv/bin/pytest -q
  pg_ctl -D /tmp/pg stop -m fast
  ```

- 스택 PR: 기준 브랜치를 앞 PR로 두면 앞 PR이 머지될 때 GitHub가 기준을 main으로 바꿉니다. 마이그레이션이 두 PR에서 같은 부모를 가리키면 나중에 머지하는 쪽 번호를 다시 매깁니다(#35 사례).
- GitHub "Re-run"은 처음 실행의 커밋을 다시 씁니다. main의 수정을 반영하려면 main을 합쳐 새로 push합니다.

## 7. 다음 세션 시작 순서

1. `git fetch && git log --oneline -1 origin/main`으로 이 문서의 §0 이후 변경이 있는지 봅니다.
2. 저장소 `CLAUDE.md`, 고칠 파일의 `python scripts/decisions.py find --path <파일>` 결과를 읽습니다.
3. §1 표에서 작업을 고르고, 근거 문서를 읽습니다. 정할 것이 남았으면 decision-interview부터 합니다.
4. 끝나면 이 문서의 해당 행을 갱신합니다.
