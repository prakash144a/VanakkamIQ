"""Platform admin screens: sign in, add a restaurant, pause it."""

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Base, Business, PlatformAdmin
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.security import hash_password
from tests.conftest import make_settings
from tests.fakes import FakeConnection
from tests.test_onboarding import valid_form

ADMIN_EMAIL, ADMIN_PASSWORD = "ops@vanakkamiq.test", "correct-horse-battery"


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def _prepare_db(url: str) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as db:
        db.add(PlatformAdmin(email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD)))
        await db.commit()
    await engine.dispose()


async def _businesses(url: str) -> list[Business]:
    engine = make_engine(url)
    async with make_session_factory(engine)() as db:
        rows = list(await db.scalars(select(Business)))
    await engine.dispose()
    return rows


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path)
    asyncio.run(_prepare_db(settings.database_url))
    with TestClient(create_app(settings, FakeConnection)) as c:
        c.db_url = settings.database_url
        yield c


def sign_in(client: TestClient) -> None:
    token = csrf_from(client.get("/platform/login").text)
    resp = client.post("/platform/login", data={"csrf": token, "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                       follow_redirects=False)
    assert resp.status_code == 303


def test_pages_require_sign_in(client):
    resp = client.get("/platform", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/platform/login"


def test_wrong_password_is_rejected(client):
    token = csrf_from(client.get("/platform/login").text)
    resp = client.post("/platform/login", data={"csrf": token, "email": ADMIN_EMAIL, "password": "nope-nope-nope"})
    assert resp.status_code == 401 and "incorrect" in resp.text


def test_forms_require_csrf_token(client):
    sign_in(client)
    resp = client.post("/platform/restaurants", data=valid_form())
    assert resp.status_code == 403


def test_add_restaurant_end_to_end(client):
    sign_in(client)
    assert "No restaurants yet" in client.get("/platform").text

    form_page = client.get("/platform/restaurants/new")
    assert form_page.status_code == 200 and "Add a restaurant" in form_page.text
    resp = client.post("/platform/restaurants", data={**valid_form(), "csrf": csrf_from(form_page.text)})
    assert resp.status_code == 200
    assert "Chennai Spice House is set up with 1 menu item" in resp.text
    assert "Temporary owner password" in resp.text
    assert "Ready for calls" in resp.text

    [business] = asyncio.run(_businesses(client.db_url))
    assert business.name == "Chennai Spice House" and business.is_active

    listing = client.get("/platform").text
    assert "Chennai Spice House" in listing and "+13125550142" in listing

    # Pause, then confirm the restaurant no longer answers.
    detail = client.get(f"/platform/restaurants/{business.id}")
    resp = client.post(f"/platform/restaurants/{business.id}/status",
                       data={"csrf": csrf_from(detail.text), "active": "0"}, follow_redirects=False)
    assert resp.status_code == 303
    [business] = asyncio.run(_businesses(client.db_url))
    assert business.is_active is False


def test_invalid_form_keeps_values_and_lists_errors(client):
    sign_in(client)
    token = csrf_from(client.get("/platform/restaurants/new").text)
    resp = client.post("/platform/restaurants", data={**valid_form(phone_number="12", menu_text="Dosa | cheap"), "csrf": token})
    assert resp.status_code == 422
    assert "Please fix 2 things" in resp.text
    assert 'value="Chennai Spice House"' in resp.text  # what was typed is kept
    assert asyncio.run(_businesses(client.db_url)) == []


def test_duplicate_phone_number_shows_error(client):
    sign_in(client)
    token = csrf_from(client.get("/platform/restaurants/new").text)
    client.post("/platform/restaurants", data={**valid_form(), "csrf": token})
    token = csrf_from(client.get("/platform/restaurants/new").text)
    resp = client.post("/platform/restaurants", data={
        **valid_form(name="Second Spot", owner_email="other@example.com"), "csrf": token,
    })
    assert resp.status_code == 422 and "already assigned to Chennai Spice House" in resp.text


async def _add_call(url: str, business_id: int) -> int:
    from app.db.models import Conversation, ConversationEvent, Order, OrderItem
    engine = make_engine(url)
    async with make_session_factory(engine)() as db:
        convo = Conversation(business_id=business_id, caller_phone="+14255550123", status="completed",
                             summary="Order #1001 confirmed: 2 x Masala Dosa. Total $25.90, pickup.")
        db.add(convo)
        await db.flush()
        db.add_all([
            ConversationEvent(conversation_id=convo.id, role="agent", text="Vanakkam! How can I help?"),
            ConversationEvent(conversation_id=convo.id, role="caller", text="Two masala dosa please"),
            ConversationEvent(conversation_id=convo.id, role="tool", tool_name="add_to_order",
                              data={"args": {"quantity": 2}, "result": {"ok": True}}),
        ])
        db.add(Order(business_id=business_id, conversation_id=convo.id, number=1001, status="confirmed",
                     customer_name="Priya", subtotal_cents=2398, tax_cents=192, total_cents=2590,
                     items=[OrderItem(name="Masala Dosa", quantity=2, unit_price_cents=1199, line_total_cents=2398,
                                      modifiers=[{"name": "Ghee roast", "price_delta_cents": 150}])]))
        await db.commit()
        cid = convo.id
    await engine.dispose()
    return cid


def test_recent_calls_and_transcript_pages(client):
    sign_in(client)
    token = csrf_from(client.get("/platform/restaurants/new").text)
    client.post("/platform/restaurants", data={**valid_form(), "csrf": token})
    [business] = asyncio.run(_businesses(client.db_url))
    cid = asyncio.run(_add_call(client.db_url, business.id))

    calls = client.get(f"/platform/restaurants/{business.id}/calls")
    assert calls.status_code == 200
    assert "Order #1001" in calls.text and "$25.90" in calls.text and "+14255550123" in calls.text

    detail = client.get(f"/platform/conversations/{cid}")
    assert detail.status_code == 200
    for text in ("Vanakkam! How can I help?", "Two masala dosa please", "add_to_order", "Masala Dosa", "Ghee roast", "Priya", "$25.90"):
        assert text in detail.text
