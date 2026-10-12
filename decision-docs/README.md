# Decision Docs

코드가 왜 지금처럼 동작하는지를 **테스트로 증명되는 결정 단위**로 기록합니다. AI 에이전트는 파일을 고치기 전에 관련 결정을 찾아 읽고, 동작이 바뀌면 기록을 함께 고칩니다.

| 위치 | 내용 |
|---|---|
| `records.jsonl` | 결정 한 줄 = 규칙·불변식·경계 계약 하나. `scope_paths`(관련 소스)와 `test_refs`(증명하는 테스트 케이스)로 연결 |
| `specs/` | 여러 결정을 묶은 **동작** 문서: "시스템이 무엇을 하는가" |
| `adrs/` | 기술 선택과 기각한 대안: "왜 이렇게 만들었는가" |
| `plans/` | `decision-interview`가 만드는 결정·검증·작업 분할 계획 |
| `issues/` | `delivery-plan`이 만드는 작업 단위 (AFK / HITL) |

## 도구

```bash
python scripts/decisions.py find --path backend/app/services/claim.py   # 이 파일을 지배하는 결정
python scripts/decisions.py find --changed                               # 내 변경과 관련된 결정
python scripts/decisions.py check --strict-paths --strict-names          # CI에서 실행
python scripts/decisions.py coverage --details                           # 결정에 연결 안 된 테스트
```

## 워크플로 (`.claude/skills/`)

```
decision-interview → verification-plan → delivery-plan → (구현: decision-map 참조) → code-review
                                                             ↘ extract-test-decisions / promote-decisions
```
