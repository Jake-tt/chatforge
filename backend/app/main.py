import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.language_models import BaseChatModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.agent.graph import build_agent_graph
from app.config import Settings, get_settings
from app.context import AppContext
from app.db import create_engine, create_sessionmaker
from app.observability import RequestContextMiddleware, configure_logging
from app.routers import auth, conversations, internal, usage
from app.services.chat_models import build_chat_model
from app.services.history import HistoryCache
from app.services.llm import LLMProvider, build_provider
from app.services.quota import QuotaService
from app.services.rag import RagPipeline
from app.services.redis_guard import RedisGuard
from app.services.search import BM25Search, SearchProvider, load_markdown_corpus
from app.services.suggestions import SuggestionService
from app.services.tasks import CloudTasksQueue, InlineTaskQueue, TaskQueue


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    llm: LLMProvider | None = None,
    tasks: TaskQueue | None = None,
    search: SearchProvider | None = None,
    chat_model: BaseChatModel | None = None,
) -> FastAPI:
    """App factory. Tests inject settings, fake Redis/LLM/search and a task queue."""
    settings = settings or get_settings()
    configure_logging(settings.log_format, settings.gcp_project)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.database_url)
        redis_client = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        queue: TaskQueue = tasks or (
            CloudTasksQueue(settings)
            if settings.task_backend == "cloudtasks"
            else InlineTaskQueue(lambda: app.state.ctx)
        )
        guard = RedisGuard(redis_client)
        provider = llm or build_provider(settings)
        rag = _build_rag(settings, provider, search)
        app.state.ctx = AppContext(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis_client,
            redis_guard=guard,
            llm=provider,
            quota=QuotaService(guard, settings.quota_timezone),
            history=HistoryCache(guard, settings.history_limit),
            rag=rag,
            suggestions=SuggestionService(provider, settings.suggestions_timeout_seconds)
            if settings.suggestions_enabled else None,
            tasks=queue,
            chat_model=chat_model or build_chat_model(settings, purpose="main"),
            agent=build_agent_graph(),
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
        expose_headers=["X-Request-ID"],
    )
    # Added last so it is the outermost layer and also times/logs CORS responses.
    app.add_middleware(RequestContextMiddleware)
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


def _build_rag(
    settings: Settings, llm: LLMProvider, search: SearchProvider | None
) -> RagPipeline:
    """The agent always routes, so there is always a pipeline; without a help center
    (RAG_ENABLED off or no documents) its help search is simply absent."""
    return RagPipeline(llm, _help_center(settings, search), settings)


def _help_center(settings: Settings, search: SearchProvider | None) -> SearchProvider | None:
    if not settings.rag_enabled:
        return None
    if search is not None:
        return search
    documents = load_markdown_corpus(settings.knowledge_dir)
    if not documents:
        logging.getLogger(__name__).warning(
            "RAG enabled but no documents in %s; help search disabled", settings.knowledge_dir
        )
        return None
    return BM25Search(documents)
