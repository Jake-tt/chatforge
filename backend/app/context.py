from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.history import HistoryCache
from app.services.llm import LLMProvider
from app.services.quota import QuotaService
from app.services.rag import RagPipeline
from app.services.redis_guard import RedisGuard
from app.services.suggestions import SuggestionService
from app.services.tasks import TaskQueue
from app.services.turn_gate import TurnGate


@dataclass
class AppContext:
    """Process-wide dependencies, built once in the lifespan and stored on app.state."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    redis_guard: RedisGuard
    llm: LLMProvider
    quota: QuotaService
    history: HistoryCache
    rag: RagPipeline
    suggestions: SuggestionService | None
    tasks: TaskQueue
    turns: TurnGate  # one turn per conversation, a few per user
    chat_model: BaseChatModel  # the agent's answering model
    agent: Any  # the compiled LangGraph graph
