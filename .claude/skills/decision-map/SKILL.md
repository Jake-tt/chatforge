---
name: decision-map
description: 코드나 테스트를 고치기 전에 그 파일을 지배하는 결정을 찾아 읽고, 동작이 바뀌면 결정 기록을 갱신하며, 소스 경로와 테스트 연결이 정확한지 검증한다. 코드 변경 전, 테스트 추가 시, 동작 변경 시, 회귀 조사 시 사용한다.
---

# Decision Map

> 원작: 정준 · 수정: jake-tt (`npm` 스크립트를 `scripts/decisions.py`로 대체)

이 저장소는 가벼운 Decision Docs 패턴을 쓴다.

- `decision-docs/records.jsonl`: 결정 저장소이자 검색 인덱스
- `scope_paths`: 결정이 지배하는 소스 경로(선별한 것, import 그래프가 아님)
- `test_refs`: 결정을 증명하는 테스트 케이스
- specs와 ADR: 상위 수준의 동작과 기술적 근거

## 코드를 고치기 전에

1. 고칠 파일마다 결정을 찾는다.

```bash
python scripts/decisions.py find --path <고칠 파일>
```

2. 나온 결정의 `summary`, `scope_paths`, 테스트, 문서를 읽는다.
3. 동작이 바뀌면 결정을 갱신하거나, 의미가 크게 바뀌면 새 `decision_id`로 대체하고 기존 것은 `superseded`로 바꾼다.
4. 관련 테스트를 돌리고 맵을 검증한다.

## 테스트를 고칠 때

1. 테스트 파일에 연결된 결정을 찾는다.

```bash
python scripts/decisions.py find --test <테스트 파일>
```

2. 테스트가 새 정책이나 불변식을 지키게 되면 결정을 추가하거나 갱신한다. 테스트 함수 이름을 바꾸면 `test_refs.names`도 고친다(CI의 `--strict-names`가 잡는다).

```json
{"path": "backend/tests/test_claim.py", "names": ["test_login_claim_is_idempotent"]}
```

3. import 기반 후보를 확인한다. 참고용이며, import된 파일을 전부 `scope_paths`에 넣지 않는다.

```bash
python scripts/decisions.py trace-tests --decision-id <DEC-...>
```

## 소스 경로가 정확한지 확인하는 세 가지 방법

1. **존재**: `python scripts/decisions.py check --strict-paths --strict-names` (지워졌거나 오타 난 경로, 사라진 테스트 이름)
2. **후보**: `python scripts/decisions.py trace-tests --decision-id <id>` (빠진 후보는 검토 신호이지 자동 수정 대상이 아님)
3. **검색**: `find --path <scope>`, `find --test <test>`. 앞으로 그 경로를 고치는 에이전트가 읽어야 할 결정이 실제로 검색되면 정확한 것이다.

## 기록 규칙

- `summary`는 구현 세부가 아니라 결정, 불변식, 경계 계약을 쓴다.
- `scope_paths`는 의미로 선별한다. 디렉터리 전체를 지배하면 `backend/app/services/`처럼 `/`로 끝낸다.
- 백필한 결정은 `status: inferred`, 검토를 마친 결정만 `accepted`.
- 지금 구현 중에 생긴 결정은 `source: new-work`.
- 상위 문서가 있으면 `doc_refs`(spec), `adr_refs`(ADR)를 단다.
- 결정이 크게 바뀌면 기존 줄을 조용히 고치지 말고 새 결정으로 대체한다.
