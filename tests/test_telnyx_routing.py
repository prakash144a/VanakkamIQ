"""6b: calls routed through the app reach OpenAI with the dialed number; staff transfers use the Voice API."""

import base64
import json
import time

import httpx
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Business, Conversation, PhoneNumber
from app.main import create_app
from app.telephony.telnyx_routing import openai_sip_uri, verify_telnyx_signature
from app.voice.base import ResponseDone
from tests.conftest import make_settings
from tests.fakes import FakeConnection
from tests.test_call_flow import ACCEPT, end_call, telnyx_translated_headers

KEY = Ed25519PrivateKey.generate()
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
TRANSFER = r"https://api\.telnyx\.com/v2/calls/.+/actions/transfer"


def signed(body: dict, ts: int | None = None) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    sig = base64.b64encode(KEY.sign(f"{ts}|".encode() + raw)).decode()
    return raw, {"telnyx-signature-ed25519": sig, "telnyx-timestamp": ts, "content-type": "application/json"}


def call_initiated(to="+13125550142", leg="v3:leg-abc") -> dict:
    return {"data": {"event_type": "call.initiated", "payload": {
        "call_control_id": leg, "direction": "incoming", "to": to, "from": "+15551230000"}}}


def test_signature_check():
    raw, headers = signed({"a": 1})
    assert verify_telnyx_signature(PUBLIC, headers["telnyx-signature-ed25519"], headers["telnyx-timestamp"], raw)
    assert not verify_telnyx_signature(PUBLIC, headers["telnyx-signature-ed25519"], headers["telnyx-timestamp"], raw + b" ")
    old_raw, old = signed({"a": 1}, ts=int(time.time()) - 3600)
    assert not verify_telnyx_signature(PUBLIC, old["telnyx-signature-ed25519"], old["telnyx-timestamp"], old_raw)
    assert not verify_telnyx_signature(PUBLIC, "not-base64!", headers["telnyx-timestamp"], raw)


def app_settings(tmp_path):
    s = make_settings(tmp_path)
    s.telnyx_public_key, s.telnyx_api_key, s.openai_project_id = PUBLIC, "KEYtest", "proj_TestProject123"
    return s


@respx.mock
def test_incoming_call_is_sent_to_openai_with_the_dialed_number(tmp_path):
    transfer = respx.post(url__regex=TRANSFER).mock(return_value=httpx.Response(200, json={"data": {}}))
    with TestClient(create_app(app_settings(tmp_path), FakeConnection)) as client:
        raw, headers = signed(call_initiated(to="+1 (312) 555-0142"))
        resp = client.post("/voice/telnyx/webhook", content=raw, headers=headers)
        assert resp.status_code == 200 and resp.json() == {"routed": True}
        bad = client.post("/voice/telnyx/webhook", content=raw, headers={**headers, "telnyx-signature-ed25519": "AAAA"})
        assert bad.status_code == 400
        raw2, headers2 = signed({"data": {"event_type": "call.hangup", "payload": {}}})
        assert client.post("/voice/telnyx/webhook", content=raw2, headers=headers2).json() == {"ignored": "call.hangup"}
    sent = json.loads(transfer.calls.last.request.content)
    assert transfer.calls.last.request.url.path == "/v2/calls/v3:leg-abc/actions/transfer"
    assert sent["to"] == openai_sip_uri("proj_TestProject123") == "sip:proj_TestProject123@sip.api.openai.com;transport=tls"
    assert {"name": "X-Called-Number", "value": "+13125550142"} in sent["custom_headers"]
    assert {"name": "X-VIQ-Leg", "value": "v3:leg-abc"} in sent["custom_headers"]


def test_webhook_refused_without_a_configured_key(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), FakeConnection)) as client:
        raw, headers = signed(call_initiated())
        assert client.post("/voice/telnyx/webhook", content=raw, headers=headers).status_code == 400


async def _two_businesses(services) -> int:
    async with services.session_factory() as db:
        demo = await db.scalar(select(Business))
        demo.handoff_phone = "+15559990000"
        second = Business(name="Second", slug="second", hours=demo.hours, is_active=True)
        db.add(second)
        await db.flush()
        db.add(PhoneNumber(business_id=second.id, e164="+15555550199"))
        await db.commit()
        return demo.id


@respx.mock
async def test_routed_call_finds_its_business_and_transfers_with_the_voice_api(services):
    from app.telephony.telnyx import TelnyxClient
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    staff = respx.post(url__regex=TRANSFER).mock(return_value=httpx.Response(200, json={"data": {}}))
    services.call_manager.telnyx = TelnyxClient(services.http, "KEYtest")
    demo_id = await _two_businesses(services)

    headers = telnyx_translated_headers("+14255550188") + [
        {"name": "X-Called-Number", "value": "+15555550100"}, {"name": "X-VIQ-Leg", "value": "v3:leg-xyz"}]
    session = await services.call_manager.handle_incoming("rtc_6b", headers)
    assert session is not None and session.business_id == demo_id  # two businesses, still the right one
    conn = services.connections[-1]
    await conn.wait_until_greeted()
    result = await conn.call_tool("transfer_to_human", reason="Wants the manager")
    assert result["ok"] is True
    await conn.push(ResponseDone(had_tool_calls=True))
    await conn.push(ResponseDone(had_tool_calls=False))
    await end_call(conn, services.call_manager.active["rtc_6b"])

    assert conn.transferred_to is None  # not via OpenAI
    assert staff.calls.last.request.url.path == "/v2/calls/v3:leg-xyz/actions/transfer"
    assert json.loads(staff.calls.last.request.content)["to"] == "+15559990000"
    async with services.session_factory() as db:
        convo = await db.scalar(select(Conversation))
    assert (convo.dialed_number, convo.routed_by) == ("+15555550100", "X-Called-Number")


@respx.mock
async def test_voice_api_transfer_failure_falls_back_to_openai(services):
    from app.telephony.telnyx import TelnyxClient
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    respx.post(url__regex=TRANSFER).mock(return_value=httpx.Response(422, json={"errors": []}))
    services.call_manager.telnyx = TelnyxClient(services.http, "KEYtest")
    await _two_businesses(services)
    headers = telnyx_translated_headers("+14255550188") + [
        {"name": "X-Called-Number", "value": "+15555550100"}, {"name": "X-VIQ-Leg", "value": "v3:leg-xyz"}]
    await services.call_manager.handle_incoming("rtc_6b2", headers)
    conn = services.connections[-1]
    await conn.wait_until_greeted()
    await conn.call_tool("transfer_to_human", reason="Wants the manager")
    await conn.push(ResponseDone(had_tool_calls=True))
    await conn.push(ResponseDone(had_tool_calls=False))
    await end_call(conn, services.call_manager.active["rtc_6b2"])
    assert conn.transferred_to == "tel:+15559990000"
