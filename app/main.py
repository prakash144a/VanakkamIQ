import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.admin import platform_routes
from app.agent.call_manager import CallManager, ConnectionFactory
from app.config import Settings, get_settings
from app.db.session import make_engine, make_session_factory
from app.events import EventBus
from app.notify.sms import Notifier
from app.telephony.telnyx import TelnyxClient
from app.voice.openai_sip import webhook
from app.voice.openai_sip.calls_api import RealtimeCallsAPI
from app.voice.openai_sip.connection import OpenAISipConnection


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker
    http: httpx.AsyncClient
    bus: EventBus
    notifier: Notifier
    calls_api: RealtimeCallsAPI
    call_manager: CallManager


def build_services(settings: Settings, connection_factory: ConnectionFactory | None = None) -> Services:
    engine = make_engine(settings.database_url, settings.database_schema)
    session_factory = make_session_factory(engine)
    http = httpx.AsyncClient()
    bus = EventBus()
    telnyx = TelnyxClient(http, settings.telnyx_api_key, settings.telnyx_base_url) if settings.telnyx_api_key else None
    notifier = Notifier(telnyx)
    calls_api = RealtimeCallsAPI(http, settings.openai_api_key, settings.openai_base_url)
    if connection_factory is None:
        def connection_factory(call_id: str) -> OpenAISipConnection:
            return OpenAISipConnection(call_id, calls_api, settings.openai_api_key, settings.openai_realtime_ws_url)
    call_manager = CallManager(
        settings=settings, session_factory=session_factory, calls_api=calls_api,
        connection_factory=connection_factory, bus=bus, notifier=notifier,
    )
    return Services(settings, engine, session_factory, http, bus, notifier, calls_api, call_manager)


def create_app(settings: Settings | None = None, connection_factory: ConnectionFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.app_env == "production":
        if settings.secret_key == "change-me" or len(settings.secret_key) < 32:
            raise RuntimeError("Set SECRET_KEY to a long random value (32+ characters) in production.")
        if settings.database_url.startswith("sqlite"):
            raise RuntimeError("Set DATABASE_URL to the PostgreSQL database in production.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services = build_services(settings, connection_factory)
        app.state.services = services
        try:
            yield
        finally:
            await services.call_manager.shutdown(settings.shutdown_grace_seconds)
            await services.http.aclose()
            await services.engine.dispose()

    app = FastAPI(title="VanakkamIQ", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, session_cookie="viq_session",
        same_site="lax", https_only=settings.app_env == "production", max_age=12 * 3600,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "admin" / "static"), name="static")
    app.include_router(webhook.router)
    app.include_router(platform_routes.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/platform")

    @app.get("/healthz")
    async def healthz() -> dict:
        async with app.state.services.session_factory() as db:
            await db.execute(text("SELECT 1"))
        return {"ok": True, "active_calls": len(app.state.services.call_manager.active)}

    return app


app = create_app()
