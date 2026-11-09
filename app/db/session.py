import re

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_schema(schema: str) -> str:
    if schema and not _SCHEMA_NAME.match(schema):
        raise ValueError(f"DATABASE_SCHEMA {schema!r} must be lowercase letters, digits and underscores.")
    return schema


def make_engine(database_url: str, schema: str = "") -> AsyncEngine:
    if database_url.startswith("sqlite"):
        return create_async_engine(database_url)
    kwargs: dict = {"pool_pre_ping": True}
    if validate_schema(schema):
        # Every connection looks up (and creates) tables in this schema only.
        kwargs["connect_args"] = {"server_settings": {"search_path": schema}}
    return create_async_engine(database_url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
