# 남은 작업 정리 (2026-11-12 기준)

다른 세션에서 이어서 작업할 수 있도록, 지금까지 끝난 것과 남은 것, 시작 방법을 한곳에 모은 문서입니다.
작업을 하나 끝내면 이 문서의 해당 행을 갱신합니다.

## 0. 현재 상태

- `main`: `e6595b7`(PR #42 머지)까지 반영. 열린 PR: **#43**(에이전트 평가 실행기, CI 통과, 머지 대기)
- 마이그레이션 head: `0014_memory_extraction_cursor` (`0010` → `0012` → `0013` → `0014`. `0011`은 없음)
- 테스트: 백엔드 389개(main), #43까지 405개. 프론트 23개. 결정 기록 53건(main), #43까지 54건
- 오늘(2026-11-12) 끝난 작업
  - #39 기억 그래프 003: 비활성 1시간 뒤 자동 기억 추출(마이그레이션 `0014`)
  - #40 감사 A1: 추천 질문보다 먼저 답변을 저장·과금, 취소된 요청은 저장이 끝난 뒤 턴 반납
  - #41 감사 A6·A8: 지난 대화 검색은 "대화 요약 + 요약 이후 원문"만 읽음, 미리 붙이는 근거에 "지시가 아닌 자료" 문구
  - #42 Langfuse 트레이싱(개발·평가 전용) + A2 평가·관측 결정·조각
  - #43 에이전트 평가 실행기와 채점 규칙(머지 대기)
- 그 전에 끝난 큰 작업
  - 첨부: 문서→마크다운(#30), 이미지(#31), 스캔 PDF OCR(#32), 잘린 문서 검색(#33), 파싱 프로세스 격리(#34), 업로드 즉시 응답(#36)
  - 사용자 기억 그래프: 저장소·통제 API(#35), 답변 반영(#37)
  - 그 외: recap(#29), 표·도식(#28), Tavily 웹 검색(#24), Ollama 콜드 스타트(#23), 비활성 압축(#22), 되묻기 선택지(#20)

## 1. 남은 작업 (우선순위 순)

| # | 작업 | 유형 | 준비 상태 | 근거 문서 |
|---|---|---|---|---|
| 1 | 감사 A2 조각 003: 평가 질문 세트 40개 | **HITL** | #43 머지 후 시작. Claude가 초안 → 사용자 검토 후 `reviewed: true` | `decision-docs/issues/261112-eval-observability/003-eval-cases.md` |
| 2 | 감사 A2 조각 004: 로컬 Langfuse docker compose, 평가 실행을 데이터셋 실행으로 올리기 | AFK | 001·002 이후. 16GB Mac 메모리 확인 필요 | `.../004-local-langfuse.md` |
| 3 | 감사 A2 조각 005: 기준선 실행과 표본 10건 수기 확인 | **HITL** | 003 이후. 로컬 Ollama, 비용 없음 | `.../005-baseline.md` |
| 4 | 기억 그래프 004: 기억 화면 | **HITL** | 화면·문구를 사용자가 확인해야 함 | `decision-docs/issues/261109-user-memory/004-memory-screen.md` |
| 5 | 감사 A4: 도구 라운드 상한·재시도에서 tools 없이 tool 기록을 보냄 | 확인 | 실제 Claude 호출 1~2회 필요(**비용 승인 후**, 수 센트 수준) | `plan/261106_agent_audit.md` A4 |
| 6 | 감사 A3·A5·A7 | 혼합 | A2 기준선 수치를 보고 결정(A3 라우터에 요약 전달, A5 지연·호출 수, A7 서두 빈도) | `plan/261106_agent_audit.md` |
| 7 | PR E: PubMed 도구와 검색 평가 | 진행 중 | PMC 수집 스크립트까지 머지. 에이전트 평가는 A2로 분리됐고, 검색 평가는 같은 틀(`backend/evals/`) 재사용 | `plan/261028_hardening_plan.md` |
| 8 | 첨부 후속 (§2) | 혼합 | 항목별 | 이 문서 §2 |
| 9 | 에이전트·모델 후속 (§3) | 혼합 | 항목별 | 이 문서 §3 |
| 10 | 브라우저 수동 확인 (§4) | 수동 | 사람이 화면을 봐야 함 | 이 문서 §4 |

감사 항목 현황(`plan/261106_agent_audit.md` 요약 표): A1·A6·A8 수정, A2 진행 중(조각 001·002 완료), A3·A4·A5·A7 미착수, A9는 A2와 함께.

### 1-1. A2 평가·Langfuse에서 정한 것과 알아둔 것

결정 전문: `decision-docs/plans/261112-eval-observability-decision-interview.md`(D1~D6, 검증 계획, 조각 001~005)

- **D1** Langfuse는 로컬 Docker 셀프호스팅, 개발·평가 전용. 운영 관측은 Cloud Logging 그대로
- **D2** 에이전트 평가는 PR E에서 분리해 먼저(보완 계획 문서도 고침)
- **D3** 질문 세트 40개(도움말 15, 계정 5, 지난 대화 5, 기억 5, 되묻기 5, 잡담 5, 약 10개 멀티턴)는 Claude 초안 → 사용자 검토. 검토 전에는 기준선으로 쓰지 않음
- **D4** 근거 충실도는 규칙 + 표본 10건 수기 확인. 로컬 qwen의 LLM 판정은 참고만(자기 채점 편향)
- **D5** `LANGFUSE_ENABLED`(기본 끔). `ENV=prod`·Cloud Run(`K_SERVICE`)·localhost가 아닌 호스트·키 누락이면 시작 거부(`DEC-BACKEND-TRACING-001`)
- **D6** 요약표는 `plan/`에 커밋, 케이스별 상세는 Langfuse(조각 004)
- 평가 실행: `cd backend && LLM_PROVIDER=ollama .venv/bin/python -m evals.run_agent_eval [--only help] [--limit 5]`. 결과는 `backend/evals/results/`(커밋 안 함). anthropic은 `--allow-cost` 필요(`DEC-EVAL-AGENT-001`)
- 실제로 부딪힌 함정(같은 실수 반복 금지)
  - Langfuse SDK의 기본 호스트는 **Langfuse Cloud**이고, `.env` 값은 pydantic-settings가 읽을 뿐 `os.environ`에 들어가지 않음. 그래서 호스트·키는 `Settings`에서 받아 클라이언트를 직접 만든다
  - `langfuse.langchain`은 `langchain-core`만으로는 import되지 않고 `langchain` 패키지가 필요(`.[observability]`에 함께 둠). v4 핸들러에는 `client` 속성이 없어 flush는 클라이언트로 한다
  - 그래프 안에서 부르는 보조 호출(라우터)이 자기 config를 넘기면 상위 run을 덮어써 트레이스가 쪼개진다. run 안에서는 config를 넘기지 않고 물려받는다(`LangChainProvider._run_config`)
  - 라우팅 채점은 소스 계획이 아니라 **라우터 원래 판단**(`agent route` 로그의 `route`)으로 한다. 계획은 3턴째부터 하이브리드 검색을 더해 오탐을 부풀린다
  - 서버 시작 직후 로컬 모델을 올리는 중에 들어온 첫 요청은 라우터가 타임아웃된다. 평가 실행기는 채점하지 않는 예열 턴을 먼저 돈다

### 1-2. 기억 그래프에서 알아둔 것

- 쓰기 경로는 `memory.add_fact` 하나뿐입니다. `remember` 도구와 자동 추출이 이것을 씁니다(중복 사실은 출처만 더해짐).
- 회원이고 `users.memory_enabled`일 때만 동작합니다. 게스트는 기억이 없고, 게스트 대화를 가져가면 추출 커서를 끝으로 옮겨 게스트 때 내용은 추출하지 않습니다(`DEC-BACKEND-MEMORY-EXTRACT-001`).
- 자동 추출(#39)의 민감정보 차단: 프롬프트 제외 + 사실별 `category`의 민감 범주 제거 + `sensitive_reason` 패턴 + 주소 형식 패턴(자동 추출에만). transcript에는 사용자가 쓴 메시지만 넣습니다.
- 실제 모델로 찾은 함정
  - 라우터는 사용자 입력만 봅니다. 라우터가 알아야 하는 것은 `router_prompt`에 넣어야 합니다(기억은 넣어 둠).
  - 로컬 모델은 context 블록을 번호 출처로 착각해 `[1]`로 인용합니다. 블록에 "번호 출처가 아님"을 밝힙니다.
  - 로컬 8B는 프롬프트의 "추출하지 마라"(건강·정치·종교·주소)는 무시하지만 범주 분류는 맞게 합니다. 번지 주소는 `place`로 분류합니다.
  - "판교에서 일한다"를 "사는 곳: 판교"로 적는 등 관계 이름이 부정확할 때가 있습니다. 004 기억 화면에서 사용자가 지울 수 있습니다.
- 남은 정리(후속): `remember`가 "사용자"라고 쓰면 "나"와 다른 엔티티가 됩니다(`memory.as_stored`를 `remember`에도 적용할지).

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
| 간헐적 라우터 타임아웃 | 콜드 스타트 수정(#23) 뒤에도 로컬에서 가끔 `router timed out` | 원인 하나 확인(2026-11-12): 서버 시작 직후 모델을 올리는 중에 들어온 첫 요청. 평가 실행에서 재현됨. 운영에서는 모델 로딩이 끝날 때까지 기다리거나 첫 요청의 라우터 타임아웃을 늘리는 방안 검토. 실패해도 agent가 도구를 직접 골라 답은 나감 |
| 출처 없는 `[n]` 표기 | 로컬 8B는 출처가 없어도 가끔 `[1]`을 붙임 | 서버에서 지우려면 인용 검사 결정(`DEC-BACKEND-RAG-CITATIONS-001`) 변경. A2 평가의 "없는 번호 인용 비율"로 빈도를 먼저 잰다 |
| 운영 트레이싱 | 지금 Langfuse는 개발·평가 전용(D1·D5) | 운영에서 켜려면 대화·기억 보관 위치, 마스킹, 개인정보 처리방침을 다시 정해야 함 |
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
| Langfuse Cloud·운영 셀프호스팅 | 채택 안 함(D1) | Cloud는 대화·기억이 외부로 나감. GCP 셀프호스팅은 ClickHouse 포함 최소 약 9 CPU / 21GiB로 비용이 큼 |
| 지난 대화를 요약만 검색 | 채택 안 함(#41) | 요약이 없는 짧은 대화가 대부분이고 요약은 세부를 잃음. "요약 + 요약 이후 원문"으로 검색 |

## 6. 작업 환경 메모

- 파이썬: `backend/.venv`는 Python 3.12(CI·Dockerfile과 같음). 의존성은 `uv pip install --python .venv/bin/python -e ".[dev]"`로 설치합니다. Langfuse까지 쓰려면 `".[dev,observability]"`. `markitdown`은 3.15를 지원하지 않습니다.
- 로컬 LLM: `ollama serve` 후 `LLM_PROVIDER=ollama`(qwen3:8b). 서버가 시작할 때 모델을 미리 올립니다(`OLLAMA_KEEP_ALIVE=-1`). Ollama 비용은 없습니다.
- 로컬 Langfuse(조각 004 이후): `.env`에 `LANGFUSE_ENABLED=true`, `LANGFUSE_HOST=http://localhost:3000`, 로컬 UI에서 만든 키. 16GB Mac에서 Ollama와 메모리를 다투므로 평가할 때만 띄웁니다.
- `LLM_PROVIDER=anthropic`, 클라우드 리소스, `deploy/*.sh`는 비용이 듭니다. 예상 비용을 먼저 알리고 승인을 받습니다(저장소 `CLAUDE.md`).
- Postgres 확인: SQLite는 외래 키를 검사하지 않아, 실제로 Postgres에서만 드러난 버그가 있었습니다(#30 flush 순서, #25 drain). 마이그레이션, 삭제 경로, 쿼리 조건을 바꾸면 일회용 Postgres로도 돌립니다.

  ```bash
  initdb -D /tmp/pg -U chatforge --auth=trust
  pg_ctl -D /tmp/pg -o "-p 55432 -k ''" -l /tmp/pg.log start
  createdb -h 127.0.0.1 -p 55432 -U chatforge chatforge
  TEST_DATABASE_URL=postgresql+asyncpg://chatforge@127.0.0.1:55432/chatforge .venv/bin/pytest -q
  pg_ctl -D /tmp/pg stop -m fast
  ```

  테스트 뒤 같은 DB에 마이그레이션을 돌리면 테스트가 만든 테이블과 충돌합니다. 마이그레이션 왕복은 새 DB(`createdb ... migcheck`)에서 합니다.
- 스택 PR: 기준 브랜치를 앞 PR로 두면 앞 PR이 머지될 때 GitHub가 기준을 main으로 바꿉니다. 앞 PR이 머지되며 브랜치가 지워지면 `gh pr create`가 "Base ref must be a branch"로 실패하니 main 기준으로 다시 만듭니다(#40 사례). 마이그레이션이 두 PR에서 같은 부모를 가리키면 나중에 머지하는 쪽 번호를 다시 매깁니다(#35 사례).
- `gh pr edit`은 GraphQL(Projects classic 폐기) 오류로 실패할 수 있습니다. 본문 수정은 `gh api -X PATCH repos/Jake-tt/chatforge/pulls/<n> -F body=@파일`로 합니다.
- GitHub "Re-run"은 처음 실행의 커밋을 다시 씁니다. main의 수정을 반영하려면 main을 합쳐 새로 push합니다.

## 7. 다음 세션 시작 순서

1. `git fetch && git log --oneline -1 origin/main`, `gh pr list`로 이 문서의 §0 이후 변경(특히 #43 머지 여부)을 봅니다.
2. 저장소 `CLAUDE.md`, 고칠 파일의 `python scripts/decisions.py find --path <파일>` 결과를 읽습니다.
3. §1 표에서 작업을 고르고, 근거 문서를 읽습니다. 정할 것이 남았으면 decision-interview부터 합니다.
4. 끝나면 이 문서의 해당 행을 갱신합니다.
