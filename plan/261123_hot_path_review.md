# 성능 관점 점검: hot path (2026-11-23)

Casey Muratori의 "Clean Code, Horrible Performance"(computerenhance.com) 관점에서 저장소를 읽고 정리한 결과입니다. 측정 없이 코드를 읽고 판단했으므로, 고치기 전후로 해당 경로를 재는 것을 원칙으로 합니다.

## 글의 주장과 이 저장소의 관계

글은 CPU에 묶인 루프에서 다형성(가상 디스패치), 잘게 쪼갠 함수, 내부 구현 은닉이 10~20배 비용을 만든다고 보이고, 데이터와 hot path를 알고 짜라고 주장합니다.

- 이 저장소는 상속 계층이 거의 없습니다. 데이터는 dataclass, 동작은 함수입니다.
- `Protocol`은 외부 경계(`SearchProvider`, `LLMProvider`, `BlobStore`)에만 있습니다. 호출 한 번이 수백 ms~수 초인 네트워크 경계라 디스패치 비용(ns)은 측정되지 않습니다. 이 경계는 fake로 테스트하는 지점이므로 switch로 바꾸지 않습니다.
- 응답 시간을 정하는 것은 I/O 왕복 수, 반복 계산, 프론트 렌더링입니다. 글의 태도는 이 축에 적용합니다.

## 발견 사항 (우선순위 순)

| # | 위치 | 문제 | 방향 | 상태 |
|---|---|---|---|---|
| ① | `routers/conversations.py` `send_message`·`_start_turn` | 첫 토큰 전에 Redis·DB 왕복이 10회 이상 순차로 일어남 | 독립 Redis 호출은 pipeline, `memory.is_enabled`+`core_block` 등은 쿼리 병합. 같은 AsyncSession은 동시 쿼리 불가라 `gather`보다 병합 | 완료(#50, [측정](261123_first_token_roundtrips.md)) |
| ② | `services/attachments.py` `images_for_turns` | 이미지 원본을 루프에서 하나씩 `await blobs.get` | `asyncio.gather`로 동시에 읽고 순서는 유지 | 완료(#49) |
| ③ | `search_attachments`, `memory.recall`, `search_past_conversations` | 검색마다 BM25 인덱스를 처음부터 만듦. 첨부는 markdown 전체를 매번 읽고 다시 나눔 | 첨부는 변환 시 passage·term frequency를 미리 저장하거나 Postgres FTS. 메모리·지난 대화는 N이 작아 보류 | 첨부 완료: 인스턴스 캐시(아래). 메모리·지난 대화는 보류 |
| ④ | `frontend/src/components/ChatPanel.tsx` | delta마다(그리고 입력 키마다) 모든 말풍선이 다시 렌더링되고, 지난 답변의 Markdown을 전부 다시 파싱. 비용이 메시지 수 × 토큰 수 | 말풍선을 `React.memo` 컴포넌트로 분리하고, 바뀌지 않은 말풍선은 props 참조가 그대로 유지되게 함 | 완료(#49) |
| ⑤ | `services/memory.py` `forget_ids`, `forget_conversations` | 행 단위 delete 루프, comprehension 안에서 set 재생성 | `IN (...)` 한 번으로. 영향 작음 | `forget_ids` 완료(아래). `forget_conversations`는 호출부가 늘 소유자 1명이라 이득 없음 |

손대지 않는 것: `AgentStream`의 holdback 합계(80자 상한), `_estimated_usage`의 문자열 결합(중단 시에만), `Protocol` 경계.

## 이번 작업 (② ④)

동작은 바뀌지 않으므로 결정 기록(`records.jsonl`)은 고치지 않습니다.

- ② 이미지 원본을 동시에 읽습니다. 한 턴에 같은 순서(`turn_order`)로 붙는 것은 그대로입니다. 테스트는 HTTP로 이미지 두 장을 보내고 blob 읽기가 동시에 진행되는지 확인합니다.
- ④ 말풍선 렌더링을 `BubbleView`(`React.memo`)로 분리합니다. 마지막 말풍선만 추천 칩 핸들러를 받아, 그 외 말풍선의 props는 렌더링 사이에 참조가 같습니다. 프론트에는 DOM 테스트 환경이 없어 의존성을 늘리지 않고, props 계산을 순수 함수(`bubbles.ts`)로 분리해 참조 유지를 테스트합니다.

## 확인 방법

- 백엔드: `cd backend && pytest -q tests/test_attachments.py`
- 프론트: `cd frontend && npm run typecheck && npm test && npm run build`
- 체감 확인: 긴 대화(답변 20개 이상)에서 React Profiler로 delta 한 번당 커밋 시간을 전후 비교합니다.

## 다음 후보

①(첫 토큰 전 왕복 줄이기)은 `send_message` 시작~첫 `delta`까지 시간을 계측한 뒤 진행합니다. ③은 첨부 크기가 커지는 시점(GCS 이전 등)에 함께 다룹니다.

## ③ 첨부 검색 (2026-11-23)

측정해 보니 비용의 거의 전부가 문단 나누기와 토큰화였습니다.

| 잘린 문서 | 문단 | 검색 1회 | 그중 질의 |
|---|---|---|---|
| 2만 자 1개 | 30 | 3.4ms | 0.04ms |
| 20만 자(상한) 1개 | 310 | 33ms | 0.3ms |
| 20만 자 4개 | 1,253 | 131ms | 1.4ms |

이벤트 루프를 막는 CPU 시간이라 같은 인스턴스의 다른 요청도 함께 기다립니다. 변환 시 DB에 미리 저장하는 방식(마이그레이션과 backfill 필요, 호출마다 읽어 인덱스를 다시 만들어야 함) 대신, 만든 인덱스를 인스턴스 메모리에 두고 다시 쓰는 방식을 골랐습니다.

- 키는 대화의 잘린 문서 id 목록입니다. 문서가 새로 잘리면 키가 바뀌어 새로 만듭니다. 캐시에 있으면 id만 읽고 markdown은 읽지 않습니다.
- 만든 때부터 15분(`attachment_index_ttl_seconds`)이 지나면 버립니다. 삭제된 문서의 본문이 메모리에 남는 시간의 상한입니다.
- 문단 수 합계 10,000(`attachment_index_max_passages`, 약 45MB)을 넘으면 가장 오래 쓰지 않은 것부터 버립니다. 인스턴스 메모리는 512Mi입니다.
- 인스턴스마다 따로 캐시하므로 새 인스턴스에서는 첫 검색에 한 번 만듭니다.

## ⑤ 기억 삭제 (2026-11-23)

- `forget_ids`(agent의 forget 도구)는 잊을 사실을 고르려고 join이 든 기억 목록 전체를 읽고, 사실마다 DELETE 두 번(사실, 참조가 끊긴 엔티티)을 했습니다. id만 읽고, 사실은 `IN (...)` 한 번, 엔티티 정리는 한 번으로 줄였습니다. 세 개를 잊으면 DELETE 6회가 2회가 됩니다.
- `forget_conversations`의 소유자별 루프는 그대로 둡니다. 호출부(대화 단건 삭제, 내 대화 전체 삭제)가 늘 한 소유자의 대화만 넘겨 루프가 한 번만 돕니다.

