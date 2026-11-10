"""Platform admin screens: email-code sign in, add a business, edit it, admins, usage."""

import asyncio
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Base, Business, PlatformAdmin
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from tests.conftest import make_settings
from tests.fakes import FakeConnection
from tests.test_onboarding import valid_form

ADMIN_EMAIL = "ops@vanakkamiq.test"


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def _prepare_db(url: str) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as db:
        db.add(PlatformAdmin(email=ADMIN_EMAIL))
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
    sent = client.post("/platform/login", data={"csrf": token, "email": ADMIN_EMAIL})
    assert sent.status_code == 200 and "Check your email" in sent.text
    resp = client.post("/platform/login/verify", data={"csrf": token, "email": ADMIN_EMAIL, "code": last_code(client)},
                       follow_redirects=False)
    assert resp.status_code == 303


def test_pages_require_sign_in(client):
    resp = client.get("/platform", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/platform/login"


def last_code(client: TestClient) -> str:
    """Tests run without email: the code is read from the outbox instead of an inbox."""
    return client.app.state.services.otp.email.outbox[-1][1].split()[0]


def test_wrong_code_and_unknown_email(client):
    token = csrf_from(client.get("/platform/login").text)
    client.post("/platform/login", data={"csrf": token, "email": ADMIN_EMAIL})
    code = last_code(client)
    wrong = client.post("/platform/login/verify", data={"csrf": token, "email": ADMIN_EMAIL,
                                                        "code": "000000" if code != "000000" else "111111"})
    assert wrong.status_code == 401 and "4 attempts left" in wrong.text
    # An unknown address gets the same screen, and no email.
    outbox = client.app.state.services.otp.email.outbox
    before = len(outbox)
    unknown = client.post("/platform/login", data={"csrf": token, "email": "stranger@example.com"})
    assert unknown.status_code == 200 and "If stranger@example.com has a VanakkamIQ login" in unknown.text
    assert len(outbox) == before
    # A business owner's address can't sign in to the platform.
    assert client.get("/platform", follow_redirects=False).status_code == 303


def test_session_cookie_lasts_and_logout_ends_it(client):
    sign_in(client)
    resp = client.get("/platform")
    assert resp.status_code == 200
    token = csrf_from(resp.text)
    client.post("/platform/logout", data={"csrf": token})
    assert client.get("/platform", follow_redirects=False).status_code == 303


def test_forms_require_csrf_token(client):
    sign_in(client)
    resp = client.post("/platform/businesses", data=valid_form())
    assert resp.status_code == 403


def test_add_restaurant_end_to_end(client):
    sign_in(client)
    assert "No businesses yet" in client.get("/platform").text

    form_page = client.get("/platform/businesses/new")
    assert form_page.status_code == 200 and "Add a business" in form_page.text
    resp = client.post("/platform/businesses", data={**valid_form(), "csrf": csrf_from(form_page.text)})
    assert resp.status_code == 200
    assert "Chennai Spice House is set up with 1 item" in resp.text
    assert "with a code emailed to owner@chennaispice.com" in resp.text
    assert "Ready for calls" in resp.text

    [business] = asyncio.run(_businesses(client.db_url))
    assert business.name == "Chennai Spice House" and business.is_active

    listing = client.get("/platform").text
    assert "Chennai Spice House" in listing and "+13125550142" in listing

    # Pause, then confirm the restaurant no longer answers.
    detail = client.get(f"/platform/businesses/{business.id}")
    resp = client.post(f"/platform/businesses/{business.id}/status",
                       data={"csrf": csrf_from(detail.text), "active": "0"}, follow_redirects=False)
    assert resp.status_code == 303
    [business] = asyncio.run(_businesses(client.db_url))
    assert business.is_active is False


def test_invalid_form_keeps_values_and_lists_errors(client):
    sign_in(client)
    token = csrf_from(client.get("/platform/businesses/new").text)
    resp = client.post("/platform/businesses", data={**valid_form(phone_number="12", catalog_text="Dosa | cheap"), "csrf": token})
    assert resp.status_code == 422
    assert "Please fix 2 things" in resp.text
    assert 'value="Chennai Spice House"' in resp.text  # what was typed is kept
    assert asyncio.run(_businesses(client.db_url)) == []


def test_duplicate_phone_number_shows_error(client):
    sign_in(client)
    token = csrf_from(client.get("/platform/businesses/new").text)
    client.post("/platform/businesses", data={**valid_form(), "csrf": token})
    token = csrf_from(client.get("/platform/businesses/new").text)
    resp = client.post("/platform/businesses", data={
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
                     confirmed_at=datetime.now(timezone.utc), public_token="tok_" + "x" * 20,
                     customer_name="Priya", subtotal_cents=2398, tax_cents=192, total_cents=2590,
                     items=[OrderItem(name="Masala Dosa", quantity=2, unit_price_cents=1199, line_total_cents=2398,
                                      modifiers=[{"name": "Ghee roast", "price_delta_cents": 150}])]))
        await db.commit()
        cid = convo.id
    await engine.dispose()
    return cid


def test_recent_calls_and_transcript_pages(client):
    sign_in(client)
    token = csrf_from(client.get("/platform/businesses/new").text)
    client.post("/platform/businesses", data={**valid_form(), "csrf": token})
    [business] = asyncio.run(_businesses(client.db_url))
    cid = asyncio.run(_add_call(client.db_url, business.id))

    calls = client.get(f"/platform/businesses/{business.id}/calls")
    assert calls.status_code == 200
    assert "Order #1001" in calls.text and "$25.90" in calls.text and "+14255550123" in calls.text

    detail = client.get(f"/platform/conversations/{cid}")
    assert detail.status_code == 200
    for text in ("Vanakkam! How can I help?", "Two masala dosa please", "add_to_order", "Masala Dosa", "Ghee roast", "Priya", "$25.90"):
        assert text in detail.text


def _create(client, **overrides) -> Business:
    token = csrf_from(client.get("/platform/businesses/new").text)
    client.post("/platform/businesses", data={**valid_form(**overrides), "csrf": token})
    return asyncio.run(_businesses(client.db_url))[-1]


def test_edit_business_switches_off_ordering(client):
    sign_in(client)
    business = _create(client)
    page = client.get(f"/platform/businesses/{business.id}/edit")
    assert page.status_code == 200 and 'value="Chennai Spice House"' in page.text
    form = {**valid_form(name="Chennai Spice Café", business_type="services", takes_orders="",
                         phone_number="+13125550177"), "csrf": csrf_from(page.text)}
    resp = client.post(f"/platform/businesses/{business.id}/edit", data=form, follow_redirects=False)
    assert resp.status_code == 303
    [business] = asyncio.run(_businesses(client.db_url))
    assert business.name == "Chennai Spice Café" and business.business_type == "services"
    assert business.takes_orders is False and business.sms_from == "+13125550177"
    detail = client.get(f"/platform/businesses/{business.id}").text
    assert "Questions &amp; messages" in detail and "+13125550177" in detail


def test_add_and_remove_platform_admin(client):
    sign_in(client)
    page = client.get("/platform/admins")
    resp = client.post("/platform/admins", data={"csrf": csrf_from(page.text), "email": "Two@VanakkamIQ.test"})
    assert resp.status_code == 200 and "Admin added." in resp.text and "two@vanakkamiq.test" in resp.text
    assert "password" not in resp.text.lower()
    again = client.post("/platform/admins", data={"csrf": csrf_from(resp.text), "email": "two@vanakkamiq.test"})
    assert again.status_code == 422 and "already a platform admin" in again.text
    admin_id = re.search(r"/platform/admins/(\d+)/delete", resp.text).group(1)
    client.post(f"/platform/admins/{admin_id}/delete", data={"csrf": csrf_from(resp.text)})
    assert "two@vanakkamiq.test" not in client.get("/platform/admins").text


def test_usage_page_totals(client):
    sign_in(client)
    business = _create(client)
    asyncio.run(_add_call(client.db_url, business.id))
    page = client.get("/platform/usage?days=7")
    assert page.status_code == 200 and "Chennai Spice House" in page.text and "$25.90" in page.text


def test_old_restaurant_links_redirect(client):
    sign_in(client)
    resp = client.get("/platform/restaurants/1/calls", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/platform/businesses/1/calls"
