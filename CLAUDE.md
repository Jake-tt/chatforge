# Chatforge 작업 규칙

AI 에이전트와 사람이 같은 기준으로 작업하기 위한 저장소 규칙입니다. 실무에서 AI 코딩 에이전트와 함께 일하며 정리한 원칙을 이 저장소에 맞게 옮겼습니다.

## 필수 규칙 (CRITICAL)

- **비용이 드는 작업은 사전 승인**: `gcloud services enable`, 클라우드 리소스 생성, `deploy/*.sh` 실행, 실제 Claude API 호출(`LLM_PROVIDER=anthropic`으로 테스트나 서버 실행)은 예상 비용을 먼저 안내하고 명시적인 승인을 받은 뒤에만 진행한다.
- **시크릿 금지**: API 키, 비밀번호, 서비스 계정 키를 코드·문서·커밋에 넣지 않는다. 운영 값은 Secret Manager, 로컬 값은 `.env`(커밋 금지)에 둔다.
- **사용자 의도에 무조건 찬성하지 않는다**: 요청이 전문가 관점에서 부적절하거나 더 나은 방식이 있으면 근거와 함께 반드시 다른 의견을 낸다. 요청의 근본 의도를 파악하고 그 의도에 더 맞는 방식을 제안한다.

## 명령어

| 목적 | 명령 |
|---|---|
| 백엔드 테스트 | `cd backend && pytest -q` (기본 SQLite, `TEST_DATABASE_URL`을 주면 Postgres) |
| 린트 | `cd backend && ruff check .` |
| 마이그레이션 검증 | `cd backend && alembic upgrade head && alembic check` |
| 프론트엔드 | `cd frontend && npm run typecheck && npm test && npm run build` |
| 로컬 서버 | `cd backend && uvicorn app.main:create_app --factory --reload` (기본 fake LLM, 비용 없음) |

## 코딩 원칙

**Clean Code**
- 함수는 한 가지 역할만 한다. 중첩을 줄이고 early return을 쓴다.
- 이름은 의도를 드러낸다. 매직 넘버 대신 이름 있는 상수나 `Settings` 값을 쓴다.
- 주석은 "왜"만 설명한다. "무엇"은 코드가 설명해야 한다.

**재사용**
- 새 코드를 쓰기 전에 기존 함수, 유틸리티, 패턴을 먼저 찾는다.
- 같은 로직이 두 곳 이상에서 실제로 반복될 때만 공통 함수로 뽑는다. 성급한 추상화는 하지 않는다.

**안티패턴 금지**
- God Object/Function, 복사-붙여넣기, 측정 없는 최적화, 이유 없이 따라 하는 패턴(Cargo Cult), 쓰이지 않는 코드(Dead Code).

## 아키텍처 규칙

- 라우트는 `deps.get_ctx`로 `AppContext`(`main.py` lifespan에서 생성)를 받는다. 라우트 안에서 엔진, Redis 클라이언트, LLM 클라이언트를 만들지 않는다.
- LLM 호출은 `services.llm.LLMProvider`를 통해서만 한다. 테스트는 `FakeProvider`나 스텁을 쓴다.
- 쿼터의 기준값은 `usage_daily`다. Redis 접근은 전부 `services.redis_guard.RedisGuard`를 거쳐 서킷 브레이커의 보호를 받는다. `KEYS`는 쓰지 않고, 패턴 삭제는 `RedisGuard.scan_delete`를 쓴다.
- 사용량을 기록하는 코드는 과금 대상 데이터와 같은 트랜잭션에서 실행하고, 커밋 후 `quota.invalidate(...)`를 호출한다.
- 백그라운드 작업은 `TaskQueue`를 거친다. 핸들러는 멱등해야 한다(at-least-once 전달).
- 보조 LLM 호출(RAG 라우팅, 출처 압축, 추천 질문)은 타임아웃과 함께 실패해도 답변은 나가도록(fail soft) 만들고, 쓴 토큰은 사용자에게 과금한다.
- system 프롬프트는 턴마다 바이트 단위로 같게 유지한다(프롬프트 캐싱). 요청마다 달라지는 내용은 `ChatTurn.context`로 현재 턴에 붙이고 저장하지 않는다.
- 쿼터 과금은 `Usage.billed(settings.token_weights())`로 한다. 원시 토큰 합계를 과금하지 않는다.
- 대화 요약은 `services.summary`가 소유한다. 경계는 user 메시지 직전에만 두고, 요약과 경계는 조건부 UPDATE 한 번으로 함께 바꾼다.
- 검색 소스는 `retrieval.gather_sources`로 병렬 실행한다. 사용자 데이터를 검색할 때는 소유자 조건을 새로 쓰지 말고 `conversations._owned_by`를 넘겨 쓴다.
- 모델 출력은 UI나 프롬프트에 들어가기 전에 검증한다(`suggestions.FollowUps`, `rag._keeps_citations` 참고). 구조화 출력은 형태만 보장하고 내용은 보장하지 않는다.
- 메시지 순서는 `created_at`이 아니라 `seq`(`_allocate_seq`로 할당)로 정한다.
- 에러는 `errors.ApiError(status, code, message)`로 낸다. 프론트엔드는 `code`로 분기한다.

## 스키마 변경

- `models.py`를 고친 뒤 `migrations/versions/NNNN_slug.py`로 마이그레이션을 추가한다.
- 모든 제약조건에 이름을 붙이고(`db.NAMING_CONVENTION`), SQLite에서도 돌도록 `batch_alter_table`을 쓴다.
- 마이그레이션 Job은 트래픽 전환 전에 실행되므로, 직전 리비전 코드와 호환돼야 한다. 순서는 expand → migrate → contract(예: `0003_message_seq.py`).
- `alembic check`가 차이를 보고하지 않아야 한다.

## 작업 방식

### 작업 슬라이싱 (구현 시작 전에 정한다)
- **Vertical (기본)**: DB, API, UI를 하나로 관통하는 최소 조각. 각 조각이 끝나면 실제로 쓸 수 있는 상태여야 한다.
- **Contract-First**: 백엔드와 프론트를 병렬로 작업할 때는 스키마(`schemas.py`, SSE 이벤트 형식)를 먼저 확정한다.
- **Risk-First**: 불확실성이 가장 큰 부분(외부 연동, 성능 가정)을 첫 조각으로 먼저 증명한다. 실패하면 이후 작업 전에 다시 설계한다.

### 커밋과 PR 크기: 의도 단위
- 줄 수가 아니라 의도로 판단한다. 기준은 **"이 커밋을 revert하면 한 가지 의도만 되돌려지는가?"**다.
- 반드시 나눈다: 의도 두 개가 섞인 경우(refactor + feature), DB 마이그레이션과 코드 변경이 섞여 부분 적용 시 위험해지는 경우.
- refactor와 feature를 섞지 않는다. 큰 refactor는 먼저 따로 머지하고 그 위에 기능을 올린다. 포맷 변경과 동작 변경도 섞지 않는다.
- "이왕 여는 김에" 주변 코드를 정리하지 않는다. 발견한 개선점은 메모만 남기고 별도 작업으로 한다.

### 롤백 친화 순서: 추가 → 전환 → 제거
- 한 커밋에서 기존 코드 삭제와 대체 구현 추가를 같이 하지 않는다. ① 새 구현 추가(기존과 공존) → ② 호출부 전환 → ③ 기존 제거 순서로, 각 단계를 독립적으로 revert할 수 있게 한다. DB도 같다(expand → migrate → contract).
- 미완성 기능은 `Settings` 플래그 뒤에 숨긴다(예: `RAG_ENABLED`, `SUGGESTIONS_ENABLED`).

### Red/Green TDD (필수)
기능을 구현할 때는 반드시 아래 순서를 따른다.
1. **RED**: 테스트를 먼저 쓰고 실행해서 실패하는 것을 확인한다. 실행하지 않고 구현으로 넘어가지 않는다. 테스트가 처음부터 통과하면 의미 없는 테스트이므로 고친다.
2. **GREEN**: 테스트를 통과하는 최소한의 구현을 쓴다.
3. **REFACTOR**: 테스트가 통과하는 상태를 유지하며 정리한다.

설정, 문서, 스타일만 바꾸는 작업은 예외다.

### 완료 전 검증 후 커밋 (Verify-Then-Commit)
1. **코드 검증**: `ruff check .`, `pytest -q`, 스키마를 바꿨다면 `alembic check`, 프론트를 바꿨다면 typecheck/test/build. 변경 전후 diff를 보고 의도하지 않은 변경이 없는지 확인한다.
2. **실제 동작 확인**: 바꾼 경로만 실제 서버를 띄워 확인한다(예: `curl -N`으로 SSE 이벤트 확인). 전체를 다시 돌릴 필요는 없다.
3. **커밋**: 둘 다 통과하면 conventional commit으로 커밋한다.

### 게이트를 건너뛰지 않는다
- 린트 실패 시 규칙을 끄지 않고 코드를 고친다.
- 테스트 실패 시 skip하지 않고 코드를 고친다.
- 경고를 무시하지 않고 원인을 해결한다.

### Mock 경계
- **외부 경계만 mock한다**: LLM(`FakeProvider`), Redis(`fakeredis`), 검색(`SearchProvider` 스텁), Cloud Tasks(기록용 큐), 시간.
- **내부 코드는 실제로 호출한다**: 라우터, 서비스, DB(테스트용 SQLite/Postgres). 동작 변경은 서비스를 직접 부르지 말고 HTTP API(`tests/conftest.py`의 `Harness`)로 테스트한다. 서킷 브레이커처럼 내부 동작 자체가 대상일 때만 예외다.

### 테스트가 실패하면
1. 에러 메시지를 정확히 읽고 원인을 파악한다.
2. 관련 변경을 diff로 확인한다.
3. 스텁과 fake의 응답이 실제 인터페이스(`LLMProvider`, `SearchProvider`)와 맞는지 확인한다.

### 에이전트 탐색 결과는 검증한다
- 서브에이전트가 "없다", "적용 안 됨"이라고 보고한 것은 직접 파일을 읽어 확인한다. 부재 주장은 오탐이 잦다.
- 에이전트 탐색은 1차 탐색일 뿐이고, 최종 정확성은 메인 에이전트가 책임진다.

### 계획 문서화
- 계획을 문서로 남겨 달라는 요청이 오면 `plan/YYMMDD_대상.md`로 저장한다.

## PR 머지 정책

GitHub 무료 플랜에서는 branch protection이 없어서 **`gh pr merge --auto`가 CI 결과를 기다리지 않고 바로 머지한다.** 실무에서 CI 시작 5초 만에 머지된 뒤 CI가 실패해 기본 브랜치가 깨진 사고가 있었고, 이후 위험도별로 머지 방식을 나눴다.

| 변경 종류 | 머지 방식 |
|---|---|
| **문서만** (`*.md`, `plan/**`, `.claude/**`) | 직접 커밋 가능 |
| **사소한 코드** (50줄 이하, 단일 파일, 테스트만, 명백히 안전) | `gh pr merge --auto --squash` 허용. 실패하면 바로 수정 커밋 |
| **위험 변경** (마이그레이션, 쿼리의 ORDER BY/JOIN 변경, 공개 시그니처, 응답 스키마, 인증·권한, 광범위 리팩터) | `gh pr checks <PR> --watch --fail-fast`로 통과를 확인한 뒤에만 머지. **`--auto` 금지** |
| **기계적 대량 변경** (dead code 삭제, codemod, 데이터 backfill) | 사람이 머지 시점을 정한다 |

- 애매하면 위험 변경으로 본다.
- 로컬 테스트 통과가 CI 통과를 뜻하지 않는다. 버전이나 환경 차이가 있으므로 CI를 watch한다.
- 이미 머지된 뒤 CI 실패를 발견하면, revert보다 수정 커밋이 빠른 경우 fix-forward하고 그 수정의 CI도 watch한다.

## 결정 기록 (Decision Docs)

- 코드나 테스트를 고치기 전에 `python scripts/decisions.py find --path <파일>`로 그 파일을 지배하는 결정을 읽는다. 상세는 `decision-map` skill.
- 동작이 바뀌면 `decision-docs/records.jsonl`도 같은 커밋에서 고친다. CI가 `check --strict-paths --strict-names`로 깨진 연결을 잡는다.
- 개요: [`decision-docs/README.md`](decision-docs/README.md)

## Skill 라우팅

| 상황 | 실행할 skill |
|---|---|
| 기존 동작을 바꾸거나 새 기능을 시작하는데 정할 것이 남아 있다 | `decision-interview` → `verification-plan` → `delivery-plan` |
| 코드·테스트를 고치기 직전 | `decision-map` |
| 결정에 연결되지 않은 테스트를 정리 | `extract-test-decisions` |
| 결정이 쌓여 문서로 정리할 때 | `promote-decisions` |
| PR 생성, push, 배포 준비 (코드 변경 포함) | `code-review`를 먼저 실행하고 PASS일 때만 진행. **PR 단위로 한 번만 실행하고, 기능 조각이나 커밋마다 실행하지 않는다** |
| 문서만 바뀐 변경 | `code-review` 생략 가능 |

## Claude 자산 추가 규칙

`.claude/agents`, `.claude/skills`, `.claude/commands`에 새 자산을 만들기 전에 아래 순서로 판단한다.

1. **매 세션 적용되는 규칙인가?** → 이 파일(CLAUDE.md)
2. **가끔 쓰는 워크플로나 지식인가?** → skill. 같은 이름의 플러그인이나 외부 자산이 없는지 먼저 확인한다(프로젝트 skill이 플러그인을 가리므로 의도하지 않은 override가 생긴다).
3. **큰 탐색이나 로그로 메인 대화 컨텍스트를 어지럽히는가?** → subagent
4. **같은 기능이 이미 있는가?** → 기존 자산을 확장한다. 기능 하나에 소유자는 하나다.

description은 3인칭 "무엇을 + 언제" 형식으로 쓴다. 자산이 쌓이면 세션 시작 시 메타데이터 토큰이 늘어나므로 주기적으로 사용량을 점검해 안 쓰는 자산을 정리한다.

## 문서 업데이트

- 설계가 바뀌면 README의 "설계 포인트"를 함께 고친다.
- 규칙이나 작업 방식이 바뀌면 이 파일을 고친다.
