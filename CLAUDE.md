# Chatforge — working notes for AI agents and humans

## Commands
- Backend tests: `cd backend && pytest -q` (SQLite; set `TEST_DATABASE_URL` for Postgres)
- Lint: `cd backend && ruff check .`
- Migrations: `cd backend && alembic upgrade head && alembic check`
- Frontend: `cd frontend && npm run typecheck && npm test && npm run build`

## Architecture rules
- Routes depend on `AppContext` (built in `main.py` lifespan) via `deps.get_ctx`; never create
  engines, Redis clients or LLM clients inside a route.
- LLM access only through `services.llm.LLMProvider`. Tests use `FakeProvider` or a stub.
- `usage_daily` is the source of truth for quotas. All Redis access goes through
  `services.redis_guard.RedisGuard` so the circuit breaker covers it. Never use `KEYS`;
  use `RedisGuard.scan_delete` for pattern deletes.
- Anything that writes usage must run in the same transaction as the data it charges for,
  then call `quota.invalidate(...)` after commit.
- Background work goes through `TaskQueue`; handlers must be idempotent (at-least-once).
- Auxiliary LLM calls (RAG routing, source compression, suggestions) must fail soft with a
  timeout — the chat answer still goes out — and their token usage is charged to the user.
- Validate model output before it reaches the UI or a prompt (see `suggestions.FollowUps`,
  `rag._keeps_citations`); structured outputs guarantee shape, not content.
- Messages are ordered by `seq` (allocated via `_allocate_seq`), never by `created_at`.
- Errors: raise `errors.ApiError(status, code, message)`; the frontend switches on `code`.

## Schema changes
- Edit `models.py`, then add a migration in `migrations/versions/` named `NNNN_slug.py`.
- Name every constraint explicitly (see `db.NAMING_CONVENTION`) and use `batch_alter_table`
  so migrations also run on SQLite.
- Migrations must stay compatible with the previously deployed revision (expand → migrate
  → contract), because the migration job runs before traffic shifts.
- `alembic check` must report no drift.

## Testing conventions
- Every behaviour change gets a test through the HTTP API (`tests/conftest.py` `Harness`),
  not by calling services directly, unless the behaviour is internal (e.g. the breaker).
