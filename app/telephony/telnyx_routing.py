"""Milestone 6b: route calls through the app so OpenAI learns which number was dialed.

Calling OpenAI's SIP address straight from a Telnyx number loses the dialed number: Telnyx's
Translated Number puts the OpenAI project ID in its place. In this mode the numbers belong to a
Telnyx Voice API (Call Control) application instead:

1. Telnyx sends `call.initiated` for each incoming call to POST /voice/telnyx/webhook (signed).
2. The app transfers the call to OpenAI's SIP address, adding two custom SIP headers:
   - X-Called-Number: the number the caller dialed (CallManager routes on it), and
   - X-VIQ-Leg: the caller's call_control_id, so a later transfer to staff can use the Voice API.
3. OpenAI then sends its usual webhook, and the call continues as before.

Numbers left on the plain SIP connection keep working (single-business fallback).
"""

import base64
import json
import logging
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request

from app.voice.sip_headers import normalize_e164

log = logging.getLogger(__name__)
router = APIRouter()

CALLED_HEADER = "X-Called-Number"
LEG_HEADER = "X-VIQ-Leg"
TOLERANCE_SECONDS = 300


def verify_telnyx_signature(public_key_b64: str, signature_b64: str, timestamp: str, body: bytes,
                            now: float | None = None) -> bool:
    """Telnyx signs `{timestamp}|{raw body}` with Ed25519; the portal shows the base64 public key."""
    try:
        if abs((now or time.time()) - int(timestamp)) > TOLERANCE_SECONDS:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), f"{timestamp}|".encode() + body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def openai_sip_uri(project_id: str) -> str:
    return f"sip:{project_id}@sip.api.openai.com;transport=tls"


@router.post("/voice/telnyx/webhook")
async def telnyx_webhook(request: Request) -> dict:
    services = request.app.state.services
    settings = services.settings
    body = await request.body()
    if not settings.telnyx_public_key or not verify_telnyx_signature(
        settings.telnyx_public_key, request.headers.get("telnyx-signature-ed25519", ""),
        request.headers.get("telnyx-timestamp", "0"), body,
    ):
        log.warning("Rejected a Telnyx webhook with a missing or bad signature")
        raise HTTPException(status_code=400, detail="bad signature")

    data = (json.loads(body or b"{}") or {}).get("data") or {}
    event, payload = data.get("event_type"), data.get("payload") or {}
    if event != "call.initiated" or payload.get("direction") != "incoming":
        return {"ignored": event}
    leg = payload.get("call_control_id") or ""
    dialed = normalize_e164(str(payload.get("to") or ""))
    if not leg or not settings.openai_project_id or services.telnyx is None:
        log.error("Can't route Telnyx call: missing call id, OPENAI_PROJECT_ID or TELNYX_API_KEY")
        return {"routed": False}
    try:
        await services.telnyx.transfer_call(leg, openai_sip_uri(settings.openai_project_id), custom_headers=[
            {"name": CALLED_HEADER, "value": dialed},
            {"name": LEG_HEADER, "value": leg},
        ])
    except Exception:  # noqa: BLE001 - logged; Telnyx then ends the unrouted call
        log.exception("Telnyx transfer to OpenAI failed")
        return {"routed": False}
    log.info("Routed Telnyx call to OpenAI with dialed number ***%s", dialed[-4:])
    return {"routed": True}
