---
name: promote-decisions
description: 쌓인 결정 기록 묶음이 spec(동작)이나 ADR(기술 선택)로 승격할 만한지 판단하고 문서를 만든다. 결정 기록을 상위 수준의 살아 있는 문서로 정리해 달라는 요청에 사용한다.
---

# Promote Decisions

> 원작: 정준 · 수정: jake-tt (chatforge의 경로와 도구에 맞게 재작성)

작은 결정 기록들을 오래 유지될 문서로 합친다. 테스트 추출과는 분리한다. `extract-test-decisions`는 케이스 단위 기록을 만들고, 이 skill은 묶음이 spec이나 ADR이 될 만큼 중요한지를 판단한다.

## 시작

```bash
python scripts/decisions.py find --query "<도메인 또는 키워드>"
```

관련 기록, 테스트, 소스, 기존 문서를 확인한다. 사용자가 명시적으로 요청하지 않으면 고립된 기록 하나로 spec이나 ADR을 만들지 않는다.

## 승격 판단

쓰기 전에 분류한다.

1. **승격 안 함**: 좁고 국소적이거나 임시이거나, 테스트와 코드만으로 이미 분명하다. `records.jsonl`에만 둔다.
2. **Spec**: "시스템이 무엇을 해야 하는가"에 답한다. 상태 전이, fallback, 검증, 라우팅 의미, 순서, 생명주기, 에러 동작, 비즈니스 규칙, 컴포넌트를 가로지르는 동작. 제품·QA·향후 에이전트가 코드를 읽지 않고도 알아야 하는 내용.
3. **ADR**: "왜 이렇게 만들었는가"에 답한다. 의미 있는 대안과 기각 이유, 인프라·라이브러리 선택, 저장 전략, 배포 전략, 보안 경계, 성능 트레이드오프. 유능한 엔지니어가 다른 방식을 제안해 다시 논쟁할 수 있는 결정.
4. **둘 다**: 같은 묶음에 동작과 아키텍처 내용이 따로 있을 때만. spec은 현재 동작을, ADR은 기술 선택과 기각한 대안을 쓴다. 구현 세부를 양쪽에 중복하지 않는다.

## 위치와 형식

- spec은 `decision-docs/specs/`, ADR은 `decision-docs/adrs/`. 문서 하나에 파일 하나, 소문자 하이픈 파일명.
- 변경 이력이 아니라 **현재 시스템**을 설명하는 살아 있는 문서로 쓴다.
- 번호 규칙이 아직 없으면 만들기 전에 묻는다.

**Spec**

```markdown
---
title: "<도메인 동작>"
description: "<검색용 설명: 동작, 핵심 규칙, 도메인 용어>"
status: accepted
tags: [domain, behavior]
---
# <도메인 동작>
## Behavior
## Related Decisions
## Verification
```

**ADR**

```markdown
---
title: "<기술 결정>"
description: "<내린 결정과 기각한 대안 하나 이상>"
status: accepted
tags: [domain, architecture]
---
# <기술 결정>
## Context
## Decision        (기각한 대안 포함)
## Consequences
## Related Decisions
```

예시: `decision-docs/specs/guest-to-member-migration.md`, `decision-docs/adrs/database-as-source-of-truth-for-quota.md`

## 기록 갱신

- spec으로 승격했으면 원 기록의 `doc_refs`에, ADR이면 `adr_refs`에 경로를 추가한다.
- `test_refs`는 그대로 둔다. 실행 가능한 증거는 계속 테스트다.

```bash
python scripts/decisions.py check --strict-paths --strict-names
```

## 멈추고 물어볼 때

- spec인지 ADR인지에 따라 문서의 독자가 달라지는데 판단이 애매할 때
- 묶음에 테스트로 증명된 근거가 부족할 때
- 서로 관계없는 결정들로 넓은 문서가 만들어질 때
- 이름이나 번호 규칙이 불분명할 때
