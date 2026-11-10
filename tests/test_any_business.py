"""Any business type: questions-only calls, closures, transcript retention, status links, migrations."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import respx
from sqlalchemy import select

from app.business_hours import closures_text, is_open
from app.business_types import TYPES, get_type, status_label
from app.db.models import Business, Conversation, ConversationEvent, Faq, Handoff, Order
from app.notify.sms import Notifier
from app.retention import DELETED_NOTE, purge_old_transcripts
from tests.test_call_flow import ACCEPT, end_call, start_call


async def _make_questions_only(services) -> None:
    async with services.session_factory() as db:
        business = await db.scalar(select(Business))
        business.business_type = "services"
        business.takes_orders = False
        business.name = "Bright Smile Dental"
        business.description = "Family dental clinic: cleanings, fillings and whitening."
        db.add(Faq(business_id=business.id, question="Do you take walk-ins?", answer="Yes, until 4 PM."))
        await db.commit()


@respx.mock
async def test_questions_only_business_gets_no_ordering_tools(services):
    await _make_questions_only(services)
    accept = respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_q", "+15551230000")

    body = json.loads(accept.calls.last.request.content)
    tools = {t["name"] for t in body["tools"]}
    assert tools == {"answer_faq", "transfer_to_human", "request_callback", "end_call"}
    instructions = body["instructions"]
    assert "receptionist for Bright Smile Dental" in instructions
    assert "Family dental clinic" in instructions and "Do you take walk-ins?" in instructions
    assert "Taking an order" not in instructions and "Chicken Biryani" not in instructions
    assert "professional judgment" in instructions  # type-specific handoff rule

    answer = await conn.call_tool("answer_faq", question="can I walk in?")
    assert answer["answers"][0]["answer"] == "Yes, until 4 PM."
    message = await conn.call_tool("request_callback", name="Ravi", message="Wants a whitening appointment")
    assert message["ok"] is True
    await end_call(conn, task)

    async with services.session_factory() as db:
        handoff = await db.scalar(select(Handoff))
        assert handoff.message == "Wants a whitening appointment" and handoff.resolved_at is None
    sent = dict(services.notifier.sent)
    assert "we got your message" in sent["+15551230000"]


@respx.mock
async def test_returning_caller_greeted_by_name_without_orders(services):
    await _make_questions_only(services)
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_1", "+15551230000")
    await conn.call_tool("request_callback", name="Ravi", message="Question about a bill")
    await end_call(conn, task)

    _, task2, conn2 = await start_call(services, "rtc_2", "+15551230000")
    assert "Vanakkam, Ravi!" in conn2.responses[0] and "last order" not in conn2.responses[0]
    await end_call(conn2, task2)


@respx.mock
async def test_custom_greeting_is_used(services):
    async with services.session_factory() as db:
        business = await db.scalar(select(Business))
        business.greeting = "Vanakkam, Demo Kitchen here. What can we make for you?"
        await db.commit()
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_g", "+15559870000")
    assert "Vanakkam, Demo Kitchen here." in conn.responses[0]
    await end_call(conn, task)


def test_holiday_closure_closes_the_whole_day():
    business = Business(timezone="America/New_York", hours={d: [["09:00", "17:00"]] for d in
                        ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                        closures=[{"date": "2026-12-25", "note": "Christmas Day"}])
    noon = lambda day: datetime(2026, 12, day, 17, 0, tzinfo=timezone.utc)  # noqa: E731 - 12:00 in New York
    assert is_open(business, noon(24)) and not is_open(business, noon(25))
    assert "Friday December 25: closed (Christmas Day)" in closures_text(business, noon(20))


def test_past_midnight_hours_use_the_previous_day():
    business = Business(timezone="UTC", hours={"fri": [["18:00", "02:00"]]}, closures=[])
    assert is_open(business, datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc))  # Friday 23:00
    assert is_open(business, datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc))  # Saturday 01:00
    assert not is_open(business, datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc))
    assert not is_open(business, datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc))  # Friday 01:00


def test_business_types_have_wording():
    for btype in TYPES.values():
        assert btype.catalog and btype.item and btype.role and btype.persona
    assert get_type("unknown").key == "other"
    assert status_label(get_type("restaurant"), "in_progress") == "Preparing"
    assert status_label(get_type("retail"), "ready", "delivery") == "Out for delivery"


async def test_retention_deletes_old_transcripts_only(services):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    async with services.session_factory() as db:
        business = await db.scalar(select(Business))
        business.transcript_retention_days = 30
        old_call = Conversation(business_id=business.id, status="completed", started_at=old,
                                summary='Caller said: "my card number is..."')
        new_call = Conversation(business_id=business.id, status="completed")
        db.add_all([old_call, new_call])
        await db.flush()
        db.add_all([ConversationEvent(conversation_id=old_call.id, role="caller", text="old words"),
                    ConversationEvent(conversation_id=new_call.id, role="caller", text="new words")])
        await db.commit()
        old_id, new_id = old_call.id, new_call.id

    assert await purge_old_transcripts(services.session_factory) == 1
    async with services.session_factory() as db:
        texts = [e.text for e in await db.scalars(select(ConversationEvent))]
        assert texts == ["new words"]
        assert (await db.get(Conversation, old_id)).summary == DELETED_NOTE
        assert (await db.get(Conversation, new_id)) is not None


def test_status_link_only_with_a_public_address():
    order = Order(public_token="abc123abc123abc123")
    assert Notifier(None, "https://viq.example.com/").status_link(order) == "https://viq.example.com/o/abc123abc123abc123"
    assert Notifier(None, "http://localhost:8000").status_link(order) is None
    assert Notifier(None, "https://viq.example.com").status_link(Order()) is None


def test_migrations_build_the_same_schema_as_the_models(tmp_path, monkeypatch):
    """Upgrade an empty database with Alembic, then compare it with the models."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from app.config import get_settings
    from app.db.models import Base

    db_file = (tmp_path / "migrated.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATABASE_SCHEMA", "")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
    engine.dispose()
    significant = [d for d in diffs if (d[0] if isinstance(d, tuple) else d[0][0]) in (
        "add_table", "remove_table", "add_column", "remove_column", "add_index", "remove_index", "modify_nullable")]
    assert significant == []
