---
name: code-review
description: main 대비 변경 사항을 범위별 전문가 에이전트로 병렬 리뷰하고, 확실한 문제는 고친 뒤 판정 리포트를 낸다. PR 생성이나 push 요청 전에 반드시 먼저 실행한다.
---

# Code Review: PR 전 AI 코드 리뷰와 품질 개선

> 원작: 정준 · 수정: jake-tt (chatforge의 경로, 명령, 규칙에 맞게 재작성)

`main` 대비 변경 사항을 리뷰하고, 발견한 문제를 고친 뒤 리포트를 출력한다.

> **CRITICAL: PR 단위로 한 번만 실행한다.**
> 기능 조각을 구현하거나 커밋할 때마다 실행하지 않는다. 브랜치의 작업을 모두 마치고 PR을 만들기(또는 push하기) 직전에 `main...HEAD` 전체를 대상으로 한 번 실행한다.
> 다시 실행하는 경우는 두 가지뿐이다. 리뷰 후 사용자 확인을 거쳐 코드가 크게 바뀌었을 때, 그리고 PR에 새 기능 커밋이 추가됐을 때다. 이때도 리뷰 이후 바뀐 커밋만 대상으로 한다.

## Step 1: 변경 범위 파악

```bash
git diff --name-only main...HEAD
git diff --name-only            # 아직 커밋하지 않은 변경
python scripts/decisions.py find --changed   # 변경 파일을 지배하는 결정
```

| 경로 패턴 | 범위 |
|---|---|
| `backend/app/**` 중 DATABASE·LLM 범위가 아닌 파일 | BACKEND |
| `backend/migrations/**`, `backend/app/models.py` | DATABASE |
| `backend/app/services/{llm,rag,search,suggestions,summary}.py`, `backend/knowledge/**` | LLM |
| `frontend/src/**` | FRONTEND |
| `decision-docs/**`, `scripts/decisions.py` | DECISIONS |
| `.github/**`, `deploy/**`, `**/Dockerfile`, `docker-compose.yml` | INFRA |

**제품 코드 diff**: 아래를 뺀 추가·삭제 줄 수. 테스트(`backend/tests/**`, `**/*.test.ts`), 문서(`*.md`), `decision-docs/records.jsonl`.

```bash
git diff --numstat main...HEAD -- . ':!backend/tests' ':!**/*.test.ts' ':!*.md' ':!decision-docs/records.jsonl'
```

## Step 2: 전문가 에이전트 병렬 실행

에이전트는 아래 **호출 조건을 만족할 때만** 실행한다. 조건을 만족하는 전문가들은 **병렬로** 실행한다. 각 에이전트에게는 세 가지를 전달한다. 자기 범위 파일의 diff(전체 diff가 아님), 변경 파일 내용, Step 1에서 찾은 관련 결정이다.

### 에이전트 없이 끝내는 경우
- 제품 코드 diff가 0줄이면(문서, 테스트, 결정 기록만 바뀐 경우) 에이전트를 실행하지 않는다. `ruff check .`와 `python scripts/decisions.py check --strict-paths --strict-names`만 실행하고 리포트를 낸다.
- 린트나 타입 오류처럼 명령으로 판정할 수 있는 항목은 에이전트에게 맡기지 않는다.

### 상시 전문가 (제품 코드 diff 50줄 이상일 때)

| 전문가 | 추가 호출 조건 |
|---|---|
| Testing | 제품 코드의 분기, 엔드포인트, 응답 형식이 바뀌었을 때. 이름 변경이나 상수 이동만 있으면 건너뛴다. |
| Maintainability | 제품 코드에서 추가된 줄이 50줄 이상일 때. 삭제 위주의 diff는 건너뛴다. |
| Testability | `routers/**`나 `services/**`에 함수가 새로 생기거나 시그니처가 바뀌었을 때 |

**Testing**
- 바뀐 동작에 대응하는 테스트가 있는가. 새 엔드포인트나 분기에 테스트가 빠졌으면 보고한다.
- 동작 변경은 서비스 직접 호출이 아니라 HTTP API(`Harness`)로 테스트했는가.
- 외부 경계(LLM, Redis, 검색, Cloud Tasks, 시간)만 mock했는가.

**Maintainability**
- **중복과 복잡도만 본다.** 같은 로직이 두 곳 이상에 반복되는지, 중첩이 깊거나 분기가 많아 읽기 어려운 함수가 있는지 확인한다.
- 쓰이지 않는 import·변수, 포맷, 이름 규칙은 보지 않는다. ruff가 잡거나 리뷰 가치가 낮기 때문이다.

**Testability (핫스팟 예방)**
- 함수가 **순수 로직과 부수효과를 섞고 있는지** 본다. 라우터 핸들러 안에서 DB 쿼리, 비즈니스 규칙, 응답 포맷팅을 다 하면 서비스로 분리를 제안한다.
- 판정 기준은 **"이 함수를 단위 테스트하려면 mock이 몇 개 필요한가?"**
  - 3개 이상 → WARNING: 순수 함수 분리 권장
  - mock 없이 테스트할 수 없는 비즈니스 로직 → CRITICAL: 반드시 분리
- 파일 300줄 초과는 보조 신호일 뿐이고, 최종 판정은 테스트 가능성으로 한다.

### 조건부 (범위 기반)

Step 1의 범위에 해당하는 파일이 바뀌었고, **그 범위의 제품 코드 diff가 10줄 이상일 때만** 실행한다. 예외로, DATABASE 범위의 마이그레이션 파일과 BACKEND 범위의 인증·권한 코드(`deps.py`, `security.py`, `routers/auth.py`, `services/claim.py`)는 한 줄만 바뀌어도 실행한다. 위험 변경이기 때문이다.

**BACKEND → Security & Architecture**
- SQL은 SQLAlchemy 표현식이나 바인딩 파라미터로만 만든다. 문자열 포매팅으로 만든 `text()`는 CRITICAL.
- 소유권 검사: 남의 리소스는 404(`_owned_by`). 인증 누락.
- 시크릿 하드코딩. 비용이 드는 호출(`gcloud services enable`, 실제 LLM 호출)이 승인 없이 들어갔는가.
- Redis는 `RedisGuard`로만, 의존성은 `AppContext`로만, 에러는 `ApiError(code)`로만.

**DATABASE → Data Migration**
- 직전 리비전과 호환되는가(expand → migrate → contract). 데이터 backfill이 필요한가.
- 제약조건 이름, `batch_alter_table`, `alembic check` 차이 없음.
- 인덱스 누락, N+1, 전체 스캔.

**LLM → Model Integration**
- 보조 LLM 호출은 타임아웃이 있고 실패해도 답변이 나가는가(fail soft). 쓴 토큰을 과금하는가.
- 모델 출력이 UI나 다음 프롬프트로 가기 전에 검증되는가. 구조화 출력은 형태만 보장한다.
- 프롬프트에 사용자 입력을 넣을 때 지시문과 데이터가 구분되는가. 출처 인용이 보존되는가.

**FRONTEND → Web Rules**
- API 호출은 `api.ts`를 통하고, 스트리밍은 `SSEParser`를 쓰는가.
- 서버 에러는 `code`로 분기하는가. 색은 `styles.css`의 CSS 변수로만 쓰고 다크 모드에서도 읽히는가.

**DECISIONS → Decision Map**
- 바뀐 동작에 해당하는 결정이 있다면 기록도 함께 바뀌었는가. 새 규칙에는 새 결정이 추가됐는가.
- `python scripts/decisions.py check --strict-paths --strict-names` 통과.

**INFRA → Deploy & CI**
- 마이그레이션 Job이 트래픽 전환 전에 실행되는 순서가 유지되는가. 시크릿은 Secret Manager로만.

## Step 3: 결과 병합과 확신도 분류

- 같은 파일, 같은 줄, 같은 분류의 finding은 하나로 합친다. 여러 전문가가 같은 문제를 보고하면 확신도를 올린다.

| 등급 | 기준 | 처리 |
|---|---|---|
| **CRITICAL** | 확실한 버그, 보안 취약점, 빌드·테스트 깨짐 | 반드시 수정 |
| **WARNING** | 높은 확률의 문제, 저장소 규칙 위반 | 수정 권장 |
| **SUGGESTION** | 개선 여지, 선택 사항 | 리포트에만 표시 |

## Step 4: Fix-First

**바로 고친다 (기계적 수정)**: 쓰이지 않는 import, 린트·포맷 문제, 하드코딩된 색을 CSS 변수로 바꾸기(대응이 명확할 때).

**사용자에게 확인한다**: 큰 리팩터가 필요한 아키텍처 수정, 해결 방향이 여러 개인 보안 문제, 테스트를 어디까지 추가할지, 트레이드오프가 있는 성능 개선.

## Step 5: 검증과 리포트

> **CRITICAL: 재검증은 Step 4에서 코드를 수정한 경우에만 실행한다.**
> 구현 단계에서 Verify-Then-Commit으로 린트와 테스트를 이미 통과했기 때문이다. 수정이 없었다면 아래 명령을 다시 돌리지 않고 바로 리포트를 낸다.
> 수정했다면 **수정한 파일의 범위에 해당하는 명령만** 실행한다(예: 프론트를 고치지 않았으면 프론트 명령은 생략).

```bash
cd backend && ruff check . && pytest -q
cd backend && alembic upgrade head && alembic check          # DATABASE 범위
cd frontend && npm run typecheck && npm test                 # FRONTEND 범위
python scripts/decisions.py check --strict-paths --strict-names
```

```
======================================================
CODE REVIEW REPORT
======================================================
Branch: feature/xxx → main
Files changed: N files (+XX/-YY lines)
Scopes: BACKEND, LLM
Decisions touched: DEC-...
------------------------------------------------------
CRITICAL (N):   [파일:줄] 분류 - 설명 → 수정 완료 / 확인 필요
WARNING (N):    [파일:줄] 분류 - 설명 → 수정 완료 / 확인 필요
SUGGESTION (N): [파일:줄] 분류 - 설명
------------------------------------------------------
AUTO-FIXED (N):     설명 (파일)
NEEDS DECISION (N): 설명 (선택지)
------------------------------------------------------
VERDICT: PASS / PASS WITH WARNINGS / NEEDS FIXES
======================================================
```

- **PASS**: CRITICAL 0건, WARNING 모두 수정
- **PASS WITH WARNINGS**: CRITICAL 0건, 일부 WARNING을 사용자가 보류
- **NEEDS FIXES**: 수정되지 않은 CRITICAL 1건 이상

PASS 또는 PASS WITH WARNINGS일 때만 PR을 만든다. 머지 방식은 CLAUDE.md의 "PR 머지 정책"을 따른다.

## 사용법

```
/code-review           # main 대비 전체 리뷰
/code-review --quick   # 상시 전문가 + Security만, SUGGESTION 생략
```
