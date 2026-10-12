import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import Settings, get_settings
from app.context import AppContext
from app.db import create_engine, create_sessionmaker
from app.routers import auth, conversations, internal, usage
from app.services.llm import LLMProvider, build_provider
from app.services.quota import QuotaService
from app.services.tasks import CloudTasksQueue, InlineTaskQueue, TaskQueue


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    llm: LLMProvider | None = None,
    tasks: TaskQueue | None = None,
) -> FastAPI:
    """App factory. Tests inject settings, a fake Redis, a fake LLM and a task queue."""
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.database_url)
        redis_client = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        queue: TaskQueue = tasks or (
            CloudTasksQueue(settings)
            if settings.task_backend == "cloudtasks"
            else InlineTaskQueue(lambda: app.state.ctx)
        )
        app.state.ctx = AppContext(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis_client,
            llm=llm or build_provider(settings),
            quota=QuotaService(redis_client, settings.quota_timezone),
            tasks=queue,
        )
        try:
            yield
        finally:
            await queue.close()
            if redis is None:
                await redis_client.aclose()
            await engine.dispose()

    app = FastAPI(title="Chatforge API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(usage.router)
    if settings.task_backend == "cloudtasks":
        app.include_router(internal.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        async with app.state.ctx.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app
