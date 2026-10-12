---
title: "게스트에서 회원으로 대화 이관"
description: "로그인 없이 대화하던 게스트가 가입하거나 로그인하면 대화와 오늘 사용량이 계정으로 옮겨지는 규칙. 멱등성, 재이관 금지, 잘못된 토큰 처리, 이관 후 게스트 토큰 거부."
status: accepted
tags: [auth, guest, quota]
---

# 게스트에서 회원으로 대화 이관

## Behavior

- 첫 방문자는 게스트 세션을 받고 로그인 없이 대화할 수 있다.
- 가입이나 로그인 요청에 `guest_token`이 함께 오면, 그 게스트의 대화 전체와 **오늘 쓴 토큰 사용량**이 한 트랜잭션으로 회원 계정에 옮겨진다. 사용량을 함께 옮기므로 "새 게스트로 대화 → 가입"으로 일일 한도를 초기화할 수 없다.
- 같은 계정으로 같은 게스트 토큰을 다시 보내면 아무것도 바뀌지 않는다(`already_claimed`). 동시에 두 번 로그인해도 행 잠금 때문에 한 번만 이관된다.
- 이미 다른 계정에 이관된 게스트는 다시 이관되지 않는다(`claimed_by_other`). 공용 기기에 남은 토큰으로 남의 대화를 가져가는 것을 막는다.
- `guest_token`이 만료됐거나, 형식이 틀렸거나, 회원 토큰이면 이관만 건너뛰고 로그인은 성공한다.
- 이관된 게스트 토큰으로 API를 호출하면 `401 guest_claimed`가 오고, 클라이언트는 새 게스트 세션을 시작한다.
- 이관된 게스트의 히스토리 캐시 키는 이관 직후 삭제된다.

## Related Decisions

- DEC-BACKEND-GUEST-CLAIM-001
- DEC-BACKEND-GUEST-CLAIM-002
- DEC-BACKEND-AUTH-401-001
- DEC-BACKEND-SCAN-DELETE-001

## Verification

- `backend/tests/test_claim.py` — 이관, 멱등성, 재이관 금지, 잘못된 토큰
- `backend/tests/test_history_cache.py::test_claim_drops_guest_cache_keys`
