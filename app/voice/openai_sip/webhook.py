"""POST /voice/openai/webhook: OpenAI tells us a call is ringing."""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.voice.webhook_signature import InvalidSignature, diagnose, verify

log = logging.getLogger(__name__)
router = APIRouter()

INCOMING_CALL_EVENTS = {"realtime.call.incoming"}


@router.post("/voice/openai/webhook")
async def openai_webhook(request: Request) -> dict:
    services = request.app.state.services
    settings = services.settings
    body = await request.body()

    if settings.openai_webhook_secret:
        try:
            verify(settings.openai_webhook_secret, request.headers, body)
        except InvalidSignature as exc:
            log.warning("Rejected webhook: %s | %s", exc, diagnose(settings.openai_webhook_secret, request.headers, body))
            raise HTTPException(status_code=400, detail="invalid signature") from exc
    elif not settings.is_dev:
        raise HTTPException(status_code=500, detail="OPENAI_WEBHOOK_SECRET is not configured")

    if services.call_manager.already_seen(request.headers.get("webhook-id")):
        return {"ok": True, "duplicate": True}

    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    if event.get("type") not in INCOMING_CALL_EVENTS:
        # Visible in the logs so a wrong webhook subscription (e.g. live.* events) is easy to spot.
        log.info("Ignoring OpenAI webhook event %r (handled: %s)", event.get("type"), sorted(INCOMING_CALL_EVENTS))
        return {"ok": True, "ignored": event.get("type")}

    data = event.get("data") or {}
    call_id = data.get("call_id")
    if not call_id:
        raise HTTPException(status_code=400, detail="missing call_id")
    await services.call_manager.handle_incoming(call_id, data.get("sip_headers") or [])
    return {"ok": True}
