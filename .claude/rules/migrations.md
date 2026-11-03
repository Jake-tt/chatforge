---
paths:
  - "backend/migrations/**/*"
  - "backend/app/{models,db}.py"
---

# 스키마 변경

- `models.py`를 고친 뒤 `migrations/versions/NNNN_slug.py`로 마이그레이션을 추가한다. 마이그레이션과 그것을 쓰는 코드 변경은 커밋을 나눈다.
- 모든 제약조건에 이름을 붙이고(`db.NAMING_CONVENTION`), SQLite에서도 돌도록 `batch_alter_table`을 쓴다.
- 마이그레이션 Job은 트래픽 전환 전에 실행되므로, 직전 리비전 코드와 호환돼야 한다. 순서는 expand → migrate → contract(예: `0003_message_seq.py`).
- 검증은 `cd backend && alembic upgrade head && alembic check`. Stop hook은 alembic을 돌리지 않으므로 직접 실행한다. `alembic check`가 차이를 보고하지 않아야 한다.
