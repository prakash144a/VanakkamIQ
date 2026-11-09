import time

import pytest

from app.voice.sip_headers import called_number_candidates, caller_display_name, caller_number, normalize_e164
from app.voice.webhook_signature import InvalidSignature, sign, verify
from tests.conftest import WEBHOOK_SECRET


def test_caller_number_from_sip_uri():
    headers = [{"name": "From", "value": '"Priya S" <sip:+15551234567@sip.telnyx.com>;tag=1'}]
    assert caller_number(headers) == "+15551234567"
    assert caller_display_name(headers) == "Priya S"


def test_caller_number_tel_uri_and_10_digit():
    assert caller_number([{"name": "from", "value": "<tel:5551234567>"}]) == "+15551234567"


def test_display_name_ignores_numbers():
    assert caller_display_name([{"name": "From", "value": '"+15551234567" <sip:+15551234567@x>'}]) is None


def test_called_number_candidates_respect_header_order():
    headers = [
        {"name": "To", "value": "<sip:proj_abc@sip.api.openai.com>"},
        {"name": "X-Called-Number", "value": "+15555550100"},
        {"name": "Diversion", "value": "<sip:+15555550199@carrier>;reason=unconditional"},
    ]
    assert called_number_candidates(headers, ["X-Called-Number", "Diversion", "To"]) == ["+15555550100", "+15555550199"]


def test_normalize_e164():
    assert normalize_e164("(555) 123-4567") == "+15551234567"
    assert normalize_e164("447700900123") == "+447700900123"


def test_signature_roundtrip():
    body = b'{"type":"realtime.call.incoming"}'
    ts = str(int(time.time()))
    headers = {"webhook-id": "wh_1", "webhook-timestamp": ts, "webhook-signature": sign(WEBHOOK_SECRET, "wh_1", ts, body)}
    verify(WEBHOOK_SECRET, headers, body)


def test_diagnose_spots_gzip_and_never_reveals_secret():
    import gzip

    from app.voice.webhook_signature import diagnose
    body = b'{"type":"realtime.call.incoming"}'
    ts = str(int(time.time()))
    headers = {"webhook-id": "wh_1", "webhook-timestamp": ts, "content-encoding": "gzip",
               "webhook-signature": sign(WEBHOOK_SECRET, "wh_1", ts, body)}
    info = diagnose(WEBHOOK_SECRET, headers, gzip.compress(body))
    assert info["matching_variants"] == ["decoded+gunzip"]
    assert WEBHOOK_SECRET not in str(info) and WEBHOOK_SECRET.removeprefix("whsec_") not in str(info)


def test_signature_rejects_tampering_and_old_timestamps():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    good = sign(WEBHOOK_SECRET, "wh_1", ts, body)
    with pytest.raises(InvalidSignature):
        verify(WEBHOOK_SECRET, {"webhook-id": "wh_1", "webhook-timestamp": ts, "webhook-signature": good}, b'{"a":2}')
    old = str(int(time.time()) - 3600)
    with pytest.raises(InvalidSignature):
        verify(WEBHOOK_SECRET, {"webhook-id": "wh_1", "webhook-timestamp": old,
                                "webhook-signature": sign(WEBHOOK_SECRET, "wh_1", old, body)}, body)
    with pytest.raises(InvalidSignature):
        verify(WEBHOOK_SECRET, {}, body)
