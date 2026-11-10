import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import Base, Business
from app.main import build_services
from app.seed import seed
from tests.fakes import FakeConnection

WEBHOOK_SECRET = "whsec_" + "dGVzdC1zZWNyZXQtZm9yLXZhbmFra2FtaXE="  # base64("test-secret-for-vanakkamiq")
DEMO_NUMBER = "+15555550100"
ALWAYS_OPEN = {d: [["00:00", "24:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        openai_api_key="sk-test",
        openai_webhook_secret=WEBHOOK_SECRET,
        telnyx_api_key="",
        hangup_delay_seconds=0,
        ai_summaries=False,  # tests that want one mock the OpenAI call (tests/test_summary.py)
        transfer_delay_seconds=0,
    )


@pytest.fixture
async def services(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED_PHONE_NUMBER", DEMO_NUMBER)
    connections: list[FakeConnection] = []

    def factory(call_id: str) -> FakeConnection:
        conn = FakeConnection(call_id)
        connections.append(conn)
        return conn

    svc = build_services(make_settings(tmp_path), factory)
    async with svc.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with svc.session_factory() as db:
        await seed(db)
        business = await db.scalar(select(Business))
        business.hours = ALWAYS_OPEN
        await db.commit()
    svc.connections = connections
    yield svc
    await svc.http.aclose()
    await svc.engine.dispose()


def sip_headers(from_number: str, to_number: str = DEMO_NUMBER, display: str | None = None) -> list[dict]:
    name = f'"{display}" ' if display else ""
    return [
        {"name": "From", "value": f"{name}<sip:{from_number}@sip.telnyx.com>;tag=abc123"},
        {"name": "To", "value": f"<sip:{to_number}@sip.api.openai.com>"},
        {"name": "Call-ID", "value": "test-call-id"},
    ]
