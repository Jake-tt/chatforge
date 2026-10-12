---
name: code-review
description: main 대비 변경 사항을 범위별 전문가 에이전트로 병렬 리뷰하고, 확실한 문제는 고친 뒤 판정 리포트를 낸다. PR 생성이나 push 요청 전에 반드시 먼저 실행한다.
---

# Code Review: PR 전 AI 코드 리뷰와 품질 개선

> 원작: 정준 · 수정: jake-tt (chatforge의 경로, 명령, 규칙에 맞게 재작성)

`main` 대비 변경 사항을 리뷰하고, 발견한 문제를 고친 뒤 리포트를 출력한다.

## Step 1: 변경 범위 파악

```bash
git diff --name-only main...HEAD
git diff --name-only            # 아직 커밋하지 않은 변경
python scripts/decisions.py find --changed   # 변경 파일을 지배하는 결정
```

| 경로 패턴 | 범위 |
|---|---|
| `backend/app/**` | BACKEND |
| `backend/migrations/**`, `backend/app/models.py` | DATABASE |
| `backend/app/services/{llm,rag,search,suggestions}.py`, `backend/knowledge/**` | LLM |
| `frontend/src/**` | FRONTEND |
| `decision-docs/**`, `scripts/decisions.py` | DECISIONS |
| `.github/**`, `deploy/**`, `**/Dockerfile`, `docker-compose.yml` | INFRA |

diff가 50줄 미만이면 Step 2의 상시 전문가만 실행하고 조건부 전문가는 건너뛴다.

## Step 2: 전문가 에이전트 병렬 실행

감지된 범위에 맞는 전문가 에이전트를 **병렬로** 실행한다. 각 에이전트에게 `git diff main...HEAD`, 변경 파일 내용, Step 1에서 찾은 관련 결정을 전달한다.

### 상시 실행 (diff 50줄 이상)

**Testing**
- 바뀐 동작에 대응하는 테스트가 있는가. 새 엔드포인트나 분기에 테스트가 빠졌으면 보고한다.
- 동작 변경은 서비스 직접 호출이 아니라 HTTP API(`Harness`)로 테스트했는가.
- 외부 경계(LLM, Redis, 검색, Cloud Tasks, 시간)만 mock했는가.

**Maintainability**
- 중복, 과도한 복잡도, 불명확한 이름, 쓰이지 않는 import·변수·코드.
- 함수 길이와 파일 크기.

**Testability (핫스팟 예방)**
- 함수가 **순수 로직과 부수효과를 섞고 있는지** 본다. 라우터 핸들러 안에서 DB 쿼리, 비즈니스 규칙, 응답 포맷팅을 다 하면 서비스로 분리를 제안한다.
- 판정 기준은 **"이 함수를 단위 테스트하려면 mock이 몇 개 필요한가?"**
  - 3개 이상 → WARNING: 순수 함수 분리 권장
  - mock 없이 테스트할 수 없는 비즈니스 로직 → CRITICAL: 반드시 분리
- 파일 300줄 초과는 보조 신호일 뿐이고, 최종 판정은 테스트 가능성으로 한다.

### 조건부 (범위 기반)

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

수정 후 다시 확인한다.

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
