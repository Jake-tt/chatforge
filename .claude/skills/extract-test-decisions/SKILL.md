---
name: extract-test-decisions
description: 기존 테스트에서 그 테스트가 증명하는 규칙을 뽑아 decision-docs/records.jsonl에 기록한다. 결정에 연결되지 않은 테스트를 백필하거나 decisions.py coverage 수치를 올릴 때 사용한다.
---

# Extract Test Decisions

> 원작: 정준 · 수정: jake-tt (pytest·vitest와 `scripts/decisions.py`에 맞게 재작성)

기존 테스트를 결정 기록으로 바꾼다. 테스트를 하나하나 기계적으로 옮기는 것이 아니라, 테스트가 증명하는 **오래 유지될 정책, 불변식, 경계 계약, 동작 결정**을 기록하는 것이 목적이다.

## 절차

1. 연결되지 않은 테스트를 범위를 좁혀 가져온다.

```bash
python scripts/decisions.py coverage --scope backend/tests/test_rag.py --details --limit 30
```

관련 없는 도메인이 섞이면 `--scope`를 더 좁히고, 이어서 볼 때는 `--offset`을 쓴다.

2. 테스트 파일과, 그 케이스를 이해하는 데 필요한 소스만 읽는다.
3. 이미 맞는 결정이 있는지 확인한다. 있으면 새 줄을 만들지 말고 그 결정의 `test_refs`에 추가한다.

```bash
python scripts/decisions.py find --test <테스트 파일>
python scripts/decisions.py find --path <소스 파일>
```

4. 알맞은 수준으로 결정을 뽑는다.
   - **좋음**: 오래 가는 규칙, 불변식, 데이터 계약, 라우팅·fallback·순서 규칙, 안전 경계, 생명주기 동작
   - **나쁨**: 테스트 이름을 다시 쓴 것, private 헬퍼 세부, 스냅샷 모양, mock 설정, 우연한 구현
   - 여러 테스트가 규칙 하나를 증명하면 `test_refs.names` 하나로 묶는다. 한 파일이 서로 다른 규칙을 증명하면 결정을 나눈다.
5. `scope_paths`는 의미로 고른다. 앞으로 그 파일을 고칠 때 이 결정이 검색돼야 하는 가장 작은 경로만 넣는다.
6. `decision-docs/records.jsonl`에 추가하거나 갱신한다.

```json
{"decision_id":"DEC-BACKEND-AREA-SLUG-001","status":"inferred","summary":"오래 유지될 결정이나 불변식을 한 문장으로.","scope_paths":["backend/app/services/x.py"],"test_refs":[{"path":"backend/tests/test_x.py","names":["test_case_name"]}],"doc_refs":[],"adr_refs":[],"source":"test-backfill","confidence":"medium"}
```

7. 검증하고 coverage가 올랐는지 확인한다.

```bash
python scripts/decisions.py check --strict-paths --strict-names
python scripts/decisions.py coverage --scope <같은 범위>
python scripts/decisions.py trace-tests --decision-id <고친 결정>   # 기존 결정을 고쳤을 때
```

## ID 규칙

- 접두사: `DEC-BACKEND-*`, `DEC-WEB-*`, `DEC-DATABASE-*`, `DEC-INFRA-*` 또는 다른 명확한 영역
- 짧은 대문자 slug와 세 자리 번호: `DEC-BACKEND-RAG-CITATIONS-001`
- 새 ID를 만들기 전에 `records.jsonl`에서 접두사와 slug를 검색한다.
- 사용자가 확인하기 전에는 `status: inferred`, `source: test-backfill`, `confidence: medium`.

## 멈추고 물어볼 때

테스트 의도가 불분명하거나, 소스 경로를 특정할 수 없거나, 테스트가 오래 유지되면 안 될 임시 동작을 검증하는 것처럼 보이면 기록하기 전에 묻는다.
