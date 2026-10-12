import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.config import get_settings
from app.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(connection: Connection | None = None, **kwargs) -> None:
    url = get_settings().database_url
    context.configure(
        connection=connection,
        url=None if connection else url,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite can't ALTER most things; batch mode recreates tables there and is a plain
        # ALTER on Postgres, so the same migrations run on both.
        render_as_batch=url.startswith("sqlite"),
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
