"""Business dashboard (/app): sign in, orders board, calls, messages, customers, catalog, answers, settings."""

import asyncio
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.caller_context import caller_context_text, load_caller_context
from app.db.models import (
    Base, Business, CatalogItem, Conversation, ConversationEvent, Customer, Faq, Handoff, Order, OrderItem, StaffUser,
)
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.onboarding.service import create_business, parse_form
from tests.conftest import make_settings
from tests.fakes import FakeConnection
from tests.test_onboarding import valid_form

OWNER = "owner@chennaispice.com"
DENTIST = "frontdesk@brightsmile.com"
CUSTOMER_PHONE = "+15551230000"


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def _prepare(url: str) -> dict:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ids: dict = {}
    async with make_session_factory(engine)() as db:
        data, _ = parse_form(valid_form(owner_email=OWNER))
        shop = (await create_business(db, data)).business

        data, _ = parse_form(valid_form(
            name="Bright Smile Dental", business_type="services", takes_orders="", phone_number="+13125550300",
            owner_email=DENTIST, faq_text="Do you take walk-ins? | Yes, until 4 PM.",
        ))
        dentist = (await create_business(db, data)).business

        customer = Customer(business_id=shop.id, phone=CUSTOMER_PHONE, name="Priya", order_count=1)
        db.add(customer)
        await db.flush()
        call = Conversation(business_id=shop.id, caller_phone=CUSTOMER_PHONE, status="active", customer_id=customer.id)
        other_call = Conversation(business_id=dentist.id, caller_phone="+15559998888", status="completed",
                                  summary="Caller asked about cleanings.")
        db.add_all([call, other_call])
        await db.flush()
        db.add_all([
            ConversationEvent(conversation_id=call.id, role="agent", text="Vanakkam! How can I help?"),
            ConversationEvent(conversation_id=call.id, role="caller", text="Two masala dosa please"),
        ])
        now = datetime.now(timezone.utc)
        order = Order(business_id=shop.id, conversation_id=call.id, customer_id=customer.id, number=1001,
                      status="confirmed", confirmed_at=now, status_updated_at=now, public_token="t" * 22,
                      customer_name="Priya", customer_phone=CUSTOMER_PHONE, subtotal_cents=2398, tax_cents=213,
                      total_cents=2611, items=[OrderItem(name="Masala Dosa", quantity=2, unit_price_cents=1199,
                                                         line_total_cents=2398, modifiers=[{"name": "Coconut"}])])
        db.add(order)
        db.add(Handoff(business_id=shop.id, conversation_id=call.id, reason="Catering for 40",
                       outcome="callback_requested", callback_name="Meena", callback_phone="+15551112222",
                       message="Catering for 40 people next Saturday"))
        await db.commit()
        ids.update(shop=shop.id, dentist=dentist.id, call=call.id, other_call=other_call.id, order=order.id,
                   customer=customer.id)
    await engine.dispose()
    return ids


async def _get(url: str, model, id_: int):
    engine = make_engine(url)
    async with make_session_factory(engine)() as db:
        row = await db.get(model, id_)
    await engine.dispose()
    return row


async def _all(url: str, query):
    engine = make_engine(url)
    async with make_session_factory(engine)() as db:
        rows = list(await db.scalars(query))
    await engine.dispose()
    return rows


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path)
    ids = asyncio.run(_prepare(settings.database_url))
    with TestClient(create_app(settings, FakeConnection)) as c:
        c.db_url, c.ids = settings.database_url, ids
        c.sms = c.app.state.services.notifier.sent
        yield c


def last_code(client: TestClient) -> str:
    return client.app.state.services.otp.email.outbox[-1][1].split()[0]


def sign_in(client: TestClient, who=OWNER) -> None:
    token = csrf_from(client.get("/app/login").text)
    client.post("/app/login", data={"csrf": token, "email": who})
    resp = client.post("/app/login/verify", data={"csrf": token, "email": who, "code": last_code(client)},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/app"


def get(client, model, id_):
    return asyncio.run(_get(client.db_url, model, id_))


# --- sign in -------------------------------------------------------------------------------
def test_dashboard_requires_sign_in(client):
    resp = client.get("/app/orders", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/app/login"
    token = csrf_from(client.get("/app/login").text)
    page = client.post("/app/login", data={"csrf": token, "email": OWNER})
    assert "Check your email" in page.text and 'autocomplete="one-time-code"' in page.text
    to, subject, text = client.app.state.services.otp.email.outbox[-1]
    assert to == OWNER and "your business dashboard" in text
    # Codes are per audience: a dashboard code doesn't open the platform.
    platform_token = csrf_from(client.get("/platform/login").text)
    resp = client.post("/platform/login/verify", data={"csrf": platform_token, "email": OWNER, "code": last_code(client)})
    assert resp.status_code == 401
    # Asking again straight away is limited, but the screen still lets them type the first code.
    again = client.post("/app/login", data={"csrf": token, "email": OWNER})
    assert again.status_code == 429 and "Please wait" in again.text and 'name="code"' in again.text


def test_questions_only_business_dashboard(client):
    sign_in(client, DENTIST)
    home = client.get("/app")
    assert home.url.path == "/app/calls" and 'href="/app/orders"' not in home.text
    assert "aren't switched on" in client.get("/app/orders").text
    assert get(client, StaffUser, 2).last_login_at is not None


# --- orders board --------------------------------------------------------------------------
def test_orders_board_moves_order_and_texts_customer(client):
    sign_in(client)
    page = client.get("/app/orders")
    assert page.status_code == 200 and "#1001" in page.text and "Masala Dosa" in page.text
    assert "#1001" in client.get("/app/orders/board").text
    token, order_id = csrf_from(page.text), client.ids["order"]

    for status in ("in_progress", "ready"):
        resp = client.post(f"/app/orders/{order_id}/status", data={"csrf": token, "status": status}, follow_redirects=False)
        assert resp.status_code == 303
    assert get(client, Order, order_id).status == "ready"
    assert any(to == CUSTOMER_PHONE and "is ready for pickup" in text for to, text in client.sms)

    client.post(f"/app/orders/{order_id}/status", data={"csrf": token, "status": "completed"})
    assert get(client, Order, order_id).status == "completed"
    assert "Picked up" in client.get("/app/orders").text  # in the last-24-hours list
    # Undoing a mistaken "picked up" doesn't text the customer again.
    before = len(client.sms)
    client.post(f"/app/orders/{order_id}/status", data={"csrf": token, "status": "ready"})
    assert len(client.sms) == before


def test_cancelling_an_order_texts_the_customer(client):
    sign_in(client)
    token = csrf_from(client.get("/app/orders").text)
    client.post(f"/app/orders/{client.ids['order']}/status", data={"csrf": token, "status": "cancelled"})
    assert get(client, Order, client.ids["order"]).status == "cancelled"
    assert any("was cancelled" in text for _, text in client.sms)


# --- calls ---------------------------------------------------------------------------------
def test_live_call_and_transcript(client):
    sign_in(client)
    calls = client.get("/app/calls")
    assert "On the phone now" in calls.text and CUSTOMER_PHONE in calls.text
    assert CUSTOMER_PHONE in client.get("/app/calls/live").text
    detail = client.get(f"/app/calls/{client.ids['call']}")
    assert "Two masala dosa please" in detail.text and "data-live-events" in detail.text
    events = client.get(f"/app/calls/{client.ids['call']}/events?after=0")
    assert events.headers["X-Call-Status"] == "active" and "Two masala dosa please" in events.text
    last_id = max(int(i) for i in re.findall(r'data-id="(\d+)"', events.text))
    assert "msg" not in client.get(f"/app/calls/{client.ids['call']}/events?after={last_id}").text


def test_other_businesses_are_invisible(client):
    sign_in(client)
    assert client.get(f"/app/calls/{client.ids['other_call']}").status_code == 404
    assert client.get(f"/app/calls/{client.ids['other_call']}/events").status_code == 404
    assert "cleanings" not in client.get("/app/calls").text
    sign_in(client, DENTIST)
    token = csrf_from(client.get("/app/calls").text)
    client.post(f"/app/orders/{client.ids['order']}/status", data={"csrf": token, "status": "cancelled"})
    assert get(client, Order, client.ids["order"]).status == "confirmed"
    assert client.get(f"/app/customers/{client.ids['customer']}").status_code == 404
    assert "Priya" not in client.get("/app/customers").text


# --- messages and customers ----------------------------------------------------------------
def test_callback_messages_can_be_handled_and_reopened(client):
    sign_in(client)
    page = client.get("/app/callbacks")
    assert "Catering for 40 people" in page.text and "+15551112222" in page.text
    handoff_id = re.search(r"/app/callbacks/(\d+)/done", page.text).group(1)
    client.post(f"/app/callbacks/{handoff_id}/done", data={"csrf": csrf_from(page.text)})
    assert "Catering for 40" not in client.get("/app/callbacks").text
    done = client.get("/app/callbacks?show=done")
    assert "Catering for 40" in done.text and OWNER in done.text
    client.post(f"/app/callbacks/{handoff_id}/done", data={"csrf": csrf_from(done.text), "undo": "1"})
    assert "Catering for 40" in client.get("/app/callbacks").text


def test_customer_notes_reach_the_ai(client):
    sign_in(client)
    assert "Priya" in client.get("/app/customers?q=pri").text
    assert "Priya" in client.get("/app/customers?q=1230000").text
    page = client.get(f"/app/customers/{client.ids['customer']}")
    assert "#1001" in page.text
    client.post(f"/app/customers/{client.ids['customer']}",
                data={"csrf": csrf_from(page.text), "name": "Priya R", "notes": "Prefers mild spice."})

    async def context():
        engine = make_engine(client.db_url)
        async with make_session_factory(engine)() as db:
            ctx = await load_caller_context(db, client.ids["shop"], CUSTOMER_PHONE)
        await engine.dispose()
        return caller_context_text(ctx)
    text = asyncio.run(context())
    assert "Priya R" in text and "Prefers mild spice." in text


# --- catalog and saved answers -------------------------------------------------------------
def test_catalog_add_edit_mark_unavailable_and_delete(client):
    sign_in(client)
    page = client.get("/app/catalog/items/new")
    bad = client.post("/app/catalog/items", data={"csrf": csrf_from(page.text), "name": "", "price": "abc",
                                                  "options": "no colon here"})
    assert bad.status_code == 422 and "Name is required" in bad.text and "Options line 1" in bad.text
    resp = client.post("/app/catalog/items", data={
        "csrf": csrf_from(page.text), "name": "Ghee Roast Dosa", "price": "13.49", "new_category": "Specials",
        "aliases": "ghee dosa", "tags": "Vegetarian", "options": "Chutney*: Coconut, Tomato +0.50", "available": "on",
    }, follow_redirects=False)
    assert resp.status_code == 303
    [item] = asyncio.run(_all(client.db_url, select(CatalogItem).where(CatalogItem.name == "Ghee Roast Dosa")))
    assert item.price_cents == 1349 and item.tags == ["vegetarian"] and item.aliases == ["ghee dosa"]
    assert item.modifier_groups[0].min_select == 1 and item.modifier_groups[0].modifiers[1].price_delta_cents == 50

    edit = client.get(f"/app/catalog/items/{item.id}")
    assert "Chutney*: Coconut, Tomato +0.50" in edit.text
    client.post(f"/app/catalog/items/{item.id}", data={
        "csrf": csrf_from(edit.text), "name": "Ghee Roast Dosa", "price": "14.00", "category_id": str(item.category_id),
        "options": "", "available": "on"})
    item = get(client, CatalogItem, item.id)
    assert item.price_cents == 1400 and item.modifier_groups == []

    catalog = client.get("/app/catalog")
    client.post(f"/app/catalog/items/{item.id}/availability", data={"csrf": csrf_from(catalog.text), "available": "0"})
    assert get(client, CatalogItem, item.id).is_available is False
    assert "Unavailable" in client.get("/app/catalog").text

    client.post(f"/app/catalog/items/{item.id}/delete", data={"csrf": csrf_from(catalog.text)})
    assert get(client, CatalogItem, item.id) is None


def test_catalog_round_trips_as_text(client):
    sign_in(client)
    page = client.get("/app/catalog/text")
    assert "Masala Dosa | 11.99 | vegetarian | Chutney*: Coconut, Tomato" in page.text
    text = "# Dosa\nMasala Dosa | 12.49 | vegetarian | Chutney*: Coconut, Tomato\nPlain Dosa | 9.99"
    client.post("/app/catalog/text", data={"csrf": csrf_from(page.text), "catalog_text": text})
    items = asyncio.run(_all(client.db_url, select(CatalogItem).where(CatalogItem.business_id == client.ids["shop"])))
    assert sorted((i.name, i.price_cents) for i in items) == [("Masala Dosa", 1249), ("Plain Dosa", 999)]
    bad = client.post("/app/catalog/text", data={"csrf": csrf_from(page.text), "catalog_text": "Dosa | cheap"})
    assert bad.status_code == 422 and len(asyncio.run(_all(client.db_url, select(CatalogItem).where(
        CatalogItem.business_id == client.ids["shop"])))) == 2


def test_saved_answers_add_edit_delete(client):
    sign_in(client)
    page = client.get("/app/faqs")
    client.post("/app/faqs", data={"csrf": csrf_from(page.text), "question": "Do you cater?", "answer": "Yes, 20+ people."})
    [faq] = asyncio.run(_all(client.db_url, select(Faq).where(Faq.question == "Do you cater?")))
    edit = client.get(f"/app/faqs?edit={faq.id}")
    client.post(f"/app/faqs/{faq.id}", data={"csrf": csrf_from(edit.text), "question": "Do you cater?",
                                             "answer": "Yes, for 20 or more people."})
    assert get(client, Faq, faq.id).answer == "Yes, for 20 or more people."
    client.post(f"/app/faqs/{faq.id}/delete", data={"csrf": csrf_from(edit.text)})
    assert get(client, Faq, faq.id) is None


# --- settings ------------------------------------------------------------------------------
def test_owner_settings_save(client):
    sign_in(client)
    page = client.get("/app/settings")
    assert page.status_code == 200 and 'value="Chennai Spice House"' in page.text
    form = {**valid_form(), "csrf": csrf_from(page.text), "closures_text": "2099-12-25 Christmas Day",
            "retention_days": "90", "greeting": "Vanakkam! Chennai Spice House, how can we help?",
            "business_type": "other", "takes_orders": ""}  # platform-only fields are ignored here
    resp = client.post("/app/settings", data=form, follow_redirects=False)
    assert resp.status_code == 303
    business = get(client, Business, client.ids["shop"])
    assert business.closures == [{"date": "2099-12-25", "note": "Christmas Day"}]
    assert business.transcript_retention_days == 90 and business.greeting.startswith("Vanakkam!")
    assert business.business_type == "restaurant" and business.takes_orders is True
    bad = client.post("/app/settings", data={**form, "tax_rate": "99"})
    assert bad.status_code == 422 and "Tax rate" in bad.text


def test_staff_role_cannot_change_settings(client):
    async def demote():
        engine = make_engine(client.db_url)
        async with make_session_factory(engine)() as db:
            user = await db.scalar(select(StaffUser).where(StaffUser.email == OWNER))
            user.role = "staff"
            await db.commit()
        await engine.dispose()
    asyncio.run(demote())
    sign_in(client)
    assert client.get("/app/settings").status_code == 403
    assert client.get("/app/catalog/items/new").status_code == 403
    assert 'href="/app/settings"' not in client.get("/app/orders").text


# --- caller's order page -------------------------------------------------------------------
def test_public_order_status_page(client):
    resp = client.get("/o/" + "t" * 22)
    assert resp.status_code == 200
    assert "Order #1001" in resp.text and "Masala Dosa" in resp.text and "We've got your order." in resp.text
    assert "set-cookie" not in resp.headers and resp.headers["cache-control"] == "no-store"
    assert CUSTOMER_PHONE not in resp.text  # the caller's own number is never shown
    assert client.get("/o/" + "x" * 22).status_code == 404
    assert client.get("/o/short").status_code == 404


def test_owner_turns_transfers_off_and_back_on(client):
    sign_in(client)
    page = client.get("/app/settings")
    assert 'name="transfers_enabled" >' in page.text  # the test business starts with transfers off
    form = {**valid_form(), "csrf": csrf_from(page.text)}
    client.post("/app/settings", data={**form, "transfers_enabled": "on"})
    assert get(client, Business, client.ids["shop"]).transfers_enabled is True
    assert 'name="transfers_enabled" checked' in client.get("/app/settings").text
    form.pop("transfers_enabled", None)  # unticked box: browsers send nothing
    client.post("/app/settings", data=form)
    assert get(client, Business, client.ids["shop"]).transfers_enabled is False


def test_return_calls_page_shows_waiting_time(client):
    sign_in(client)
    page = client.get("/app/callbacks")
    assert "Return calls" in page.text and "Waiting " in page.text and "Mark called back" in page.text
    assert 'aria-label="1 waiting for a call back"' in client.get("/app/calls").text
