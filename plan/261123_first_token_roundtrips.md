# 첫 토큰 전 왕복 줄이기 (2026-11-23)

[hot path 점검](261123_hot_path_review.md)의 ①입니다. 메시지를 보낸 뒤 스트리밍이 시작되기 전(`TurnResponse` 생성 시점)까지 Postgres 문장·커밋과 Redis 명령·pipeline을 왕복 1회로 셌습니다. 측정은 `backend/tests/test_turn_roundtrips.py`가 하며, 같은 대화의 두 번째 메시지 기준입니다.

## 결과

| | 전 | 후 |
|---|---|---|
| 게스트 | 15 | 12 |
| 회원(기억 켬) | 17 | 13 |

| 변경 | 줄어든 왕복 |
|---|---|
| `memory_enabled`를 인증 때 읽는 users 행에서 가져옴(`Principal`) | 회원 SQL 1 |
| 파일이 붙은 최근 user 턴이 없으면 이미지 조회 생략 | SQL 1 |
| 메시지 rate limit과 사용량 캐시 GET을 한 pipeline으로(`QuotaService.check_turn`) | Redis 1 |
| user 메시지 history 추가와 최근 history 읽기를 한 pipeline으로(`HistoryCache.append_and_recent`) | Redis 1 |

모두 동작은 그대로입니다. rate limit은 fail-open, 사용량은 캐시가 없거나 Redis가 없으면 DB, history는 seq 완결성 검사에 실패하면 DB에서 다시 만듭니다.

## 남은 왕복 (게스트 12)

인증 SELECT, 대화 소유 확인 SELECT, rate limit·사용량 pipeline, 사용량 DB 읽기와 캐시 SET, 턴 잠금 SET NX, 동시 스트림 pipeline, seq 할당 UPDATE, 메시지 INSERT, COMMIT, history pipeline, 잘린 문서 확인 SELECT. 회원은 기억 core_block SELECT가 더해집니다.

## 결정이 필요한 것: 사용량 캐시가 채팅 경로에서 거의 맞지 않음

답변을 저장할 때마다 `quota.invalidate`로 사용량 캐시를 지우므로(DEC-BACKEND-QUOTA-SOT-001의 cache-aside), 다음 메시지는 항상 캐시를 놓치고 DB 읽기와 SET을 합니다(왕복 2회). 줄이려면 커밋 후 새 합계를 캐시에 쓰는 방식 등을 검토해야 하는데, quota 문서가 경합 때문에 무효화를 고른 배경이 있어 결정 기록을 먼저 바꿔야 합니다. 이번에는 손대지 않습니다.

## 그 밖의 후보

- 턴 잠금 SET NX와 동시 스트림 pipeline 합치기: 잠금 실패 시 되돌리기가 필요해 복잡도 대비 이득이 작습니다.
- 잘린 문서 확인 SELECT: 대화에 문서 첨부가 있었는지 대화 행에 표시해 두면 생략할 수 있지만 마이그레이션이 필요합니다.
- Postgres(asyncpg)에서는 트랜잭션 BEGIN도 왕복이라 실제 수치는 이보다 큽니다. 테스트는 SQLite로 셉니다.
