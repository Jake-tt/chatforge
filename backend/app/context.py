from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.llm import LLMProvider
from app.services.quota import QuotaService
from app.services.tasks import TaskQueue


@dataclass
class AppContext:
    """Process-wide dependencies, built once in the lifespan and stored on app.state."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    llm: LLMProvider
    quota: QuotaService
    tasks: TaskQueue
