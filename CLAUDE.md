# Chatforge 작업 규칙

AI 에이전트와 사람이 같은 기준으로 작업하기 위한 저장소 규칙입니다. 실무에서 AI 코딩 에이전트와 함께 일하며 정리한 원칙을 이 저장소에 맞게 옮겼습니다.

<!--
유지보수 메모 (HTML 주석은 Claude 컨텍스트에 로드되지 않음)
- 예외 없이 지켜야 하는 규칙은 이 파일이 아니라 .claude/settings.json(권한 규칙·hook)과 GitHub ruleset에 둔다.
  이 파일은 모델이 판단할 지침만 담는다. 공식 권장: https://code.claude.com/docs/en/best-practices
- 특정 경로에만 해당하는 규칙은 .claude/rules/에 paths로 범위를 정해 둔다(migrations.md, llm-agent.md).
- 머지 정책의 배경: 실무에서 branch protection이 없는 저장소에 `gh pr merge --auto --squash`로 올린 PR이
  CI 시작 5초 만에 머지되고 CI가 실패해 기본 브랜치가 깨졌다(2026-05-09). 이 저장소는 public이라
  무료 플랜에서도 ruleset을 쓸 수 있어, 문서 규칙 대신 필수 상태 체크로 막는다.
-->

## 원칙

- 비용이 드는 작업(클라우드 리소스 생성, `deploy/*.sh`, `LLM_PROVIDER=anthropic`으로 테스트나 서버 실행)은 예상 비용을 먼저 안내하고 승인을 받는다. 해당 명령은 hook이 승인 창을 띄운다.
- API 키, 비밀번호, 서비스 계정 키를 코드·문서·커밋에 넣지 않는다. 운영 값은 Secret Manager, 로컬 값은 `.env`(커밋 금지, 읽기 차단)에 둔다.
- 요청이 전문가 관점에서 부적절하거나 더 나은 방식이 있으면 근거와 함께 다른 의견을 낸다. 요청의 근본 의도에 더 맞는 방식을 제안한다.

## 명령어

| 목적 | 명령 |
|---|---|
| 백엔드 테스트 | `cd backend && pytest -q` (기본 SQLite, `TEST_DATABASE_URL`을 주면 Postgres) |
| 테스트 하나 | `cd backend && pytest -q tests/test_chat.py::<테스트 이름>` |
| 린트 | `cd backend && ruff check .` |
| 마이그레이션 검증 | `cd backend && alembic upgrade head && alembic check` |
| 프론트엔드 | `cd frontend && npm run typecheck && npm test && npm run build` |
| 로컬 서버 | `cd backend && uvicorn app.main:create_app --factory --reload` (기본 fake LLM, 비용 없음) |

턴을 끝낼 때 Stop hook(`.claude/hooks/verify.sh`)이 바뀐 영역의 ruff, pytest, `decisions.py check`, 프론트 typecheck/test를 실행하고, 실패하면 종료를 막는다.

## 아키텍처 규칙

- 라우트는 `deps.get_ctx`로 `AppContext`(`main.py` lifespan에서 생성)를 받는다. 라우트 안에서 엔진, Redis 클라이언트, LLM 클라이언트를 만들지 않는다.
- 답변 모델은 `services.chat_models.build_chat_model`의 LangChain 채팅 모델, 보조 호출(라우팅·압축·추천 질문·제목·요약)은 `services.llm.LLMProvider`를 통해서만 한다. 테스트는 답변에 `tests/fakes.py`의 `ScriptedChatModel`·`RecordingChatModel`, 보조 호출에 `FakeProvider`나 스텁을 쓴다.
- 쿼터의 기준값은 `usage_daily`다. Redis 접근은 전부 `services.redis_guard.RedisGuard`를 거친다. `KEYS`는 쓰지 않고, 패턴 삭제는 `RedisGuard.scan_delete`를 쓴다.
- 사용량은 과금 대상 데이터와 같은 트랜잭션에서 기록하고, 커밋 후 `quota.invalidate(...)`를 호출한다. 과금은 `Usage.billed(settings.token_weights())`로 하고, 원시 토큰 합계를 과금하지 않는다.
- 백그라운드 작업은 `TaskQueue`를 거친다. 핸들러는 멱등해야 한다(at-least-once 전달).
- 사용자 데이터를 조회할 때는 소유자 조건을 새로 쓰지 말고 `conversations._owned_by`를 쓴다.
- 메시지 순서는 `created_at`이 아니라 `seq`(`_allocate_seq`로 할당)로 정한다.
- 에러는 `errors.ApiError(status, code, message)`로 낸다. 프론트엔드는 `code`로 분기한다.

## 작업 방식

- **커밋은 의도 단위**: 기준은 "이 커밋을 revert하면 한 가지 의도만 되돌려지는가?"다. refactor와 feature, 포맷 변경과 동작 변경을 섞지 않는다. "이왕 여는 김에" 주변 코드를 정리하지 않고 메모만 남긴다.
- **추가 → 전환 → 제거**: 기존 코드 삭제와 대체 구현 추가를 한 커밋에 넣지 않는다. 미완성 기능은 `Settings` 플래그 뒤에 숨긴다(예: `RAG_ENABLED`).
- **Red/Green TDD**: 기능 구현은 실패하는 테스트를 먼저 쓰고 실제로 실행해 실패를 확인한 뒤 구현한다. 처음부터 통과하는 테스트는 고친다. 설정, 문서, 스타일만 바꾸는 작업은 예외다.
- **실제 동작 확인**: 바꾼 경로만 서버를 띄워 확인한다(예: `curl -N`으로 SSE 이벤트 확인).
- **게이트를 우회하지 않는다**: 린트·테스트가 실패하면 규칙을 끄거나 skip하지 말고 코드를 고친다.
- **Mock 경계**: 외부 경계만 mock한다. LLM(`ScriptedChatModel`·`RecordingChatModel`·`FakeProvider`), Redis(`fakeredis`), 검색(`SearchProvider` 스텁), Cloud Tasks(기록용 큐), 시간. 동작 변경은 서비스를 직접 부르지 말고 HTTP API(`tests/conftest.py`의 `Harness`)로 테스트한다. 서킷 브레이커처럼 내부 동작 자체가 대상일 때만 예외다.
- 테스트가 실패하면 스텁과 fake가 실제 인터페이스(LangChain `BaseChatModel`, `LLMProvider`, `SearchProvider`)와 맞는지도 확인한다.
- 서브에이전트가 "없다", "적용 안 됨"이라고 보고한 것은 직접 파일을 읽어 확인한다. 부재 주장은 오탐이 잦다.
- 계획을 문서로 남겨 달라는 요청은 `plan/YYMMDD_대상.md`(gitignore된 개인 메모)에 저장한다. skill이 만드는 계획과 작업 단위는 `decision-docs/plans/`, `issues/`에 둔다.

## PR 머지 정책

- `main`은 GitHub ruleset으로 보호한다. PR 필수, CI(`decisions`, `backend`, `frontend`) 통과 필수, force push 금지. 문서만 바꾼 변경도 PR로 올린다.
- 머지는 사용자가 한다(`gh pr merge`는 승인 창이 뜬다). 필수 체크가 있으므로 `--auto`는 CI를 기다린다.
- 위험 변경(마이그레이션, 쿼리의 ORDER BY/JOIN 변경, 공개 시그니처, 응답 스키마, 인증·권한, 광범위 리팩터)과 기계적 대량 변경(dead code 삭제, codemod, backfill)은 PR 설명에 위험 지점을 적는다. 애매하면 위험 변경으로 본다.
- 머지된 뒤 CI 실패를 발견하면, revert보다 수정이 빠른 경우 fix-forward하고 그 수정의 CI도 확인한다.

## 결정 기록 (Decision Docs)

- 코드나 테스트를 고치기 전에 `python scripts/decisions.py find --path <파일>`로 그 파일을 지배하는 결정을 읽는다. 상세는 `decision-map` skill.
- 동작이 바뀌면 `decision-docs/records.jsonl`도 같은 커밋에서 고친다. CI와 Stop hook이 `check --strict-paths --strict-names`로 깨진 연결을 잡는다.
- 개요: [`decision-docs/README.md`](decision-docs/README.md)

## Skill 라우팅

| 상황 | 실행할 skill |
|---|---|
| 기존 동작을 바꾸거나 새 기능을 시작하는데 정할 것이 남아 있다 | `decision-interview` → `verification-plan` → `delivery-plan` |
| 코드·테스트를 고치기 직전 | `decision-map` |
| 결정에 연결되지 않은 테스트를 정리 | `extract-test-decisions` |
| 결정이 쌓여 문서로 정리할 때 | `promote-decisions` |
| 코드 변경이 포함된 PR 생성이나 push 직전 | `pr-review`를 PR 단위로 한 번 실행하고 PASS일 때만 진행. 문서만 바뀐 변경은 생략 |

## Claude 자산 추가 규칙

`.claude/` 아래에 새 자산을 만들기 전에 아래 순서로 판단한다.

1. **예외 없이 지켜야 하는가?** → `.claude/settings.json`의 권한 규칙이나 hook. 선호에 가까운 규칙은 hook으로 만들지 않는다.
2. **매 세션 적용되는 지침인가?** → 이 파일. 특정 경로에만 해당하면 `.claude/rules/`에 `paths`로 범위를 정한다.
3. **가끔 쓰는 워크플로나 지식인가?** → skill. 내장 skill이나 플러그인과 이름이 겹치지 않는지 먼저 확인한다(프로젝트 skill이 같은 이름을 가린다).
4. **큰 탐색이나 로그로 메인 대화 컨텍스트를 어지럽히는가?** → subagent
5. **같은 기능이 이미 있는가?** → 기존 자산을 확장한다. 기능 하나에 소유자는 하나다.

코드를 읽어서 알 수 있는 내용은 적지 않는다. description은 3인칭 "무엇을 + 언제" 형식으로 쓴다.

## 문서 업데이트

- 설계가 바뀌면 README의 "설계 포인트"를 함께 고친다.
- 규칙이나 작업 방식이 바뀌면 이 파일을 고친다.
