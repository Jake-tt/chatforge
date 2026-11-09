import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.language_models import BaseChatModel
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy import text

from app.agent.graph import build_agent_graph
from app.config import Settings, get_settings
from app.context import AppContext
from app.db import create_engine, create_sessionmaker
from app.observability import RequestContextMiddleware, configure_logging
from app.routers import auth, conversations, internal, payments, usage
from app.routers.conversations import drain_unfinished_writes
from app.services.attachments import LocalBlobStore
from app.services.chat_models import build_chat_model, warm_up_ollama
from app.services.history import HistoryCache
from app.services.llm import LLMProvider, build_provider
from app.services.quota import QuotaService
from app.services.rag import RagPipeline
from app.services.redis_guard import RedisGuard
from app.services.search import BM25Search, SearchProvider, load_markdown_corpus
from app.services.suggestions import SuggestionService
from app.services.tasks import CloudTasksQueue, InlineTaskQueue, TaskQueue
from app.services.turn_gate import TurnGate
from app.services.web_search import TavilySearch, WebSearchProvider


def build_redis(settings: Settings) -> Redis:
    # redis-py 8 defaults to a 5 s socket timeout: a hung Redis would add that to every
    # request in flight before the breaker opens, and again to each one after a cooldown.
    return Redis.from_url(
        settings.redis_url, decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        # Explicit, because `Redis(...)` (unlike `from_url`) retries 10 times by default:
        # each retry would add another timeout. The breaker is the retry policy.
        retry=Retry(NoBackoff(), 0),
    )


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    llm: LLMProvider | None = None,
    tasks: TaskQueue | None = None,
    search: SearchProvider | None = None,
    chat_model: BaseChatModel | None = None,
    web_search: WebSearchProvider | None = None,
) -> FastAPI:
    """App factory. Tests inject settings, fake Redis/LLM/search and a task queue."""
    settings = settings or get_settings()
    configure_logging(settings.log_format, settings.gcp_project)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.database_url)
        redis_client = redis or build_redis(settings)
        queue: TaskQueue = tasks or (
            CloudTasksQueue(settings)
            if settings.task_backend == "cloudtasks"
            else InlineTaskQueue(lambda: app.state.ctx)
        )
        guard = RedisGuard(redis_client, failure_threshold=settings.redis_failure_threshold,
                           cooldown_seconds=settings.redis_cooldown_seconds)
        provider = llm or build_provider(settings)
        rag = _build_rag(settings, provider, search)
        tavily = TavilySearch(settings.tavily_api_key.get_secret_value(),
                              timeout_seconds=settings.web_search_timeout_seconds) \
            if settings.web_search_enabled and web_search is None else None
        app.state.ctx = AppContext(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis_client,
            redis_guard=guard,
            llm=provider,
            quota=QuotaService(guard, settings.quota_timezone),
            history=HistoryCache(guard, settings.history_limit,
                                 settings.attachment_inline_chars),
            blobs=LocalBlobStore(settings.attachments_dir),
            rag=rag,
            suggestions=SuggestionService(provider, settings.suggestions_timeout_seconds)
            if settings.suggestions_enabled else None,
            tasks=queue,
            # The lock outlives the turn timeout, so it only expires for a lost instance.
            turns=TurnGate(guard, int(settings.agent_turn_timeout_seconds) + 60,
                           settings.max_concurrent_streams),
            chat_model=chat_model or build_chat_model(settings, purpose="main"),
            agent=build_agent_graph(),
            web_search=(web_search or tavily) if settings.web_search_enabled else None,
        )
        # In the background: the server can take requests while the model loads, and a
        # request that arrives first simply waits for the same load.
        warm_up = asyncio.create_task(warm_up_ollama(settings)) \
            if settings.llm_provider == "ollama" and chat_model is None else None
        try:
            yield
        finally:
            if warm_up is not None:
                warm_up.cancel()
            # Saves for cancelled requests first: they need the database and Redis below.
            # Bounded, within Cloud Run's 10s grace period after SIGTERM.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(8.0):
                    await drain_unfinished_writes()
            await queue.close()
            if tavily is not None:
                await tavily.close()
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
    if settings.payment_webhook_secret:
        app.include_router(payments.router)

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
