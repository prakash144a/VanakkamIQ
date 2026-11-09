import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text

from app.config import get_settings
from app.db.models import Base
from app.db.session import make_engine, validate_schema

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = get_settings()
schema = validate_schema(settings.database_schema) if not settings.database_url.startswith("sqlite") else ""


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url, target_metadata=target_metadata, literal_binds=True,
        version_table_schema=schema or None,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    if schema:
        # Our own schema inside a shared database; existing tables elsewhere are never touched.
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True,
        version_table_schema=schema or None,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(settings.database_url, schema)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
