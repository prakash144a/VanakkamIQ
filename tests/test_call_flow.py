"""End-to-end call handling with a fake OpenAI control socket and mocked OpenAI HTTP calls."""

import asyncio
import json

import httpx
import respx
from sqlalchemy import select

from app.db.models import Business, Conversation, ConversationEvent, Customer, Handoff, Order
from app.voice.base import CallerTranscript, Closed, ResponseDone
from tests.conftest import sip_headers

ACCEPT = r"https://api\.openai\.com/v1/realtime/calls/.+/accept"
REJECT = r"https://api\.openai\.com/v1/realtime/calls/.+/reject"


async def start_call(services, call_id: str, from_number: str, **kw):
    session = await services.call_manager.handle_incoming(call_id, sip_headers(from_number, **kw))
    task = services.call_manager.active.get(call_id)
    conn = services.connections[-1] if session else None
    if conn is not None:
        await conn.wait_until_greeted()
    return session, task, conn


async def end_call(conn, task):
    await conn.push(Closed())
    await asyncio.wait_for(task, timeout=5)


@respx.mock
async def test_new_caller_places_pickup_order(services):
    accept = respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    session, task, conn = await start_call(services, "rtc_1", "+15551230000")
    assert session is not None and conn.opened

    body = json.loads(accept.calls.last.request.content)
    assert body["type"] == "realtime"
    assert {t["name"] for t in body["tools"]} >= {"search_catalog", "add_to_order", "confirm_order", "transfer_to_human"}
    assert "Chicken Biryani" in body["instructions"]
    assert conn.responses[0].startswith("Greet the caller") and "Vanakkam!" in conn.responses[0]

    await conn.push(CallerTranscript("Hi, can I get two chicken biriyani please?"))
    found = await conn.call_tool("search_catalog", query="chicken biriyani")
    item_id = found["matches"][0]["item_id"]
    assert found["matches"][0]["name"] == "Chicken Biryani"

    missing = await conn.call_tool("add_to_order", item_id=item_id, quantity=2)
    assert missing["ok"] is False and "Spice level" in missing["needs"][0]

    added = await conn.call_tool("add_to_order", item_id=item_id, quantity=2, options=["medium", "extra raita"])
    assert added["ok"] is True
    # (15.99 + 1.50) x 2 = 34.98, 8% tax = 2.80
    assert added["order"]["total"] == "$37.78"

    early = await conn.call_tool("confirm_order", customer_name="Priya", fulfillment="pickup", caller_confirmed_readback=False)
    assert early["ok"] is False

    confirmed = await conn.call_tool("confirm_order", customer_name="Priya", fulfillment="pickup",
                                     requested_time="7:15 PM", caller_confirmed_readback=True)
    assert confirmed["ok"] is True and confirmed["order_number"] == 1001
    await conn.push(ResponseDone(had_tool_calls=True))

    await conn.call_tool("end_call")
    await conn.push(ResponseDone(had_tool_calls=True))
    await conn.push(ResponseDone(had_tool_calls=False))  # goodbye spoken -> hang up
    await end_call(conn, task)

    assert conn.hung_up
    assert len([r for r in conn.responses if r is None]) == 2  # a response after each tool round
    async with services.session_factory() as db:
        convo = await db.scalar(select(Conversation))
        assert convo.status == "completed"
        assert "Order #1001 confirmed" in convo.summary
        roles = [e.role for e in await db.scalars(select(ConversationEvent))]
        assert "caller" in roles and "tool" in roles
        customer = await db.scalar(select(Customer))
        assert customer.name == "Priya" and customer.order_count == 1
        order = await db.scalar(select(Order))
        assert order.status == "confirmed" and order.customer_id == customer.id
    texts = dict(services.notifier.sent)
    assert "Order #1001 confirmed" in texts["+15551230000"]


@respx.mock
async def test_returning_caller_gets_personal_greeting(services):
    accept = respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_1", "+15551230000")
    found = await conn.call_tool("search_catalog", query="mango lassi")
    await conn.call_tool("add_to_order", item_id=found["matches"][0]["item_id"], quantity=1)
    await conn.call_tool("confirm_order", customer_name="Priya", fulfillment="pickup", caller_confirmed_readback=True)
    await end_call(conn, task)

    _, task2, conn2 = await start_call(services, "rtc_2", "+15551230000")
    assert "Vanakkam, Priya!" in conn2.responses[0]
    assert "1 x Mango Lassi" in conn2.responses[0]
    assert "Returning caller: Priya" in json.loads(accept.calls.last.request.content)["instructions"]
    await end_call(conn2, task2)


@respx.mock
async def test_transfer_to_staff(services):
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    async with services.session_factory() as db:
        business = await db.scalar(select(Business))
        business.handoff_phone = "+15559990000"
        await db.commit()

    _, task, conn = await start_call(services, "rtc_1", "+15551230000")
    result = await conn.call_tool("transfer_to_human", reason="Caller asked for the manager")
    assert result["ok"] is True
    await conn.push(ResponseDone(had_tool_calls=True))
    await conn.push(ResponseDone(had_tool_calls=False))
    await end_call(conn, task)

    assert conn.transferred_to == "tel:+15559990000"
    async with services.session_factory() as db:
        assert (await db.scalar(select(Conversation))).status == "escalated"
        assert (await db.scalar(select(Handoff))).outcome == "transferring"


@respx.mock
async def test_no_staff_number_offers_callback(services):
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_1", "+15551230000")
    result = await conn.call_tool("transfer_to_human", reason="Allergy question")
    assert result["ok"] is False and "request_callback" in result["instruction"]
    callback = await conn.call_tool("request_callback", name="Meena", message="Is the dosa batter nut-free?")
    assert callback["ok"] is True
    await end_call(conn, task)
    async with services.session_factory() as db:
        handoff = await db.scalar(select(Handoff))
        assert handoff.outcome == "callback_requested" and handoff.callback_phone == "+15551230000"
        assert (await db.scalar(select(Conversation))).status == "escalated"


@respx.mock
async def test_unknown_number_is_rejected_when_fallback_is_off(services):
    services.settings.single_business_fallback = False
    reject = respx.post(url__regex=REJECT).mock(return_value=httpx.Response(200))
    session, _, _ = await start_call(services, "rtc_x", "+15551230000", to_number="+15550000000")
    assert session is None
    assert reject.called


def telnyx_translated_headers(from_number: str) -> list[dict]:
    """What Telnyx really sends when the Translated Number is the OpenAI project ID."""
    return [
        {"name": "From", "value": f'"{from_number[2:]}" <sip:{from_number[2:]}@sip.telnyx.eu>;tag=x'},
        {"name": "To", "value": "<sip:proj_TestProject123@sip.api.openai.com:5061>;tag=y"},
        {"name": "P-Asserted-Identity", "value": f'"{from_number[2:]}"<sip:{from_number[2:]}@sip.telnyx.com;verstat=TN-Validation-Passed>'},
    ]


@respx.mock
async def test_single_restaurant_answers_when_dialed_number_is_hidden(services):
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    session = await services.call_manager.handle_incoming("rtc_t", telnyx_translated_headers("+14255550188"))
    assert session is not None
    assert session.ctx.caller_phone == "+14255550188"
    conn = services.connections[-1]
    await conn.wait_until_greeted()
    await end_call(conn, services.call_manager.active["rtc_t"])


@respx.mock
async def test_two_restaurants_never_guess(services):
    from app.db.models import PhoneNumber
    reject = respx.post(url__regex=REJECT).mock(return_value=httpx.Response(200))
    async with services.session_factory() as db:
        second = Business(name="Second", slug="second", hours={}, is_active=True)
        db.add(second)
        await db.flush()
        db.add(PhoneNumber(business_id=second.id, e164="+15555550199"))
        await db.commit()
    session = await services.call_manager.handle_incoming("rtc_t2", telnyx_translated_headers("+14255550188"))
    assert session is None and reject.called


@respx.mock
async def test_unconfirmed_order_marks_call_abandoned(services):
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_1", "+15551230000")
    found = await conn.call_tool("search_catalog", query="masala dosa")
    await conn.call_tool("add_to_order", item_id=found["matches"][0]["item_id"], quantity=1)
    await end_call(conn, task)
    async with services.session_factory() as db:
        assert (await db.scalar(select(Conversation))).status == "abandoned"
        assert (await db.scalar(select(Order))).status == "cancelled"
