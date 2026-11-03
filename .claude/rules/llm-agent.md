---
paths:
  - "backend/app/agent/**/*"
  - "backend/app/services/{llm,chat_models,rag,retrieval,search,suggestions,summary,past_conversations}.py"
  - "backend/knowledge/**/*"
---

# LLM·에이전트 코드

- 보조 LLM 호출(RAG 라우팅, 출처 압축, 추천 질문)은 타임아웃을 두고, 실패해도 답변은 나가도록(fail soft) 만든다. 쓴 토큰은 사용자에게 과금한다.
- system 프롬프트는 턴마다 바이트 단위로 같게 유지한다(프롬프트 캐싱). 요청마다 달라지는 내용은 `ChatTurn.context`로 현재 턴에 붙이고 저장하지 않는다.
- 대화 요약은 `services.summary`가 소유한다. 경계는 user 메시지 직전에만 두고, 요약과 경계는 조건부 UPDATE 한 번으로 함께 바꾼다.
- 검색 소스는 `retrieval.gather_sources`로 병렬 실행한다.
- 에이전트 그래프의 노드는 `Runtime[AgentContext]`로 요청별 의존성을 받는다. 노드에서 모델이나 DB 세션을 직접 만들지 않는다. 새 도구는 `agent/tools.py`의 `Tool`로 만들고, 실패는 예외 대신 `ToolOutput.failed`로 모델에게 돌려준다.
- 모델 출력은 UI나 프롬프트에 들어가기 전에 검증한다(`suggestions.FollowUps`, `rag._keeps_citations` 참고). 구조화 출력은 형태만 보장하고 내용은 보장하지 않는다.
