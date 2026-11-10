"""Bring a person in: live transfer when staff are available, otherwise take a callback message."""

from app.agent.tools import ToolContext
from app.business_hours import is_open
from app.db.models import Business, Conversation, Handoff
from app.voice.sip_headers import normalize_e164

CALL_BACK_PROMISE = (
    "Tell the caller warmly that a team member will call them back in a few minutes. Get their name, confirm "
    "the best number to reach them (they may use the number they're calling from) and what it's about, "
    "then call request_callback."
)


def can_transfer(business: Business) -> bool:
    """Live transfers need the owner's switch on and a staff number to ring."""
    return bool(business.transfers_enabled and business.handoff_phone)


async def start_transfer(ctx: ToolContext, reason: str) -> dict:
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        if not can_transfer(business):
            return {"ok": False, "reason": "this business doesn't transfer calls", "instruction": CALL_BACK_PROMISE}
        if not is_open(business):
            why = "we're closed right now" if business.handoff_phone else "no team member is available by phone"
            return {
                "ok": False,
                "reason": why,
                "instruction": (
                    f"Explain kindly that {why}. Offer to take a message: get their name, confirm the best "
                    "callback number (they may use the number they are calling from), and what it is about, "
                    "then call request_callback."
                ),
            }
        target = f"tel:{business.handoff_phone}"
        convo = await db.get(Conversation, ctx.conversation_id)
        convo.status = "escalated"
        db.add(Handoff(
            business_id=business.id, conversation_id=ctx.conversation_id,
            reason=reason, outcome="transferring", target=target,
        ))
        await db.commit()

    ctx.state["escalated"] = True
    ctx.call.request_transfer(target)
    await ctx.notifier.escalation(business, ctx.caller_phone, reason)
    ctx.bus.publish(ctx.business_id, {"type": "call.escalated", "conversation_id": ctx.conversation_id, "reason": reason})
    return {
        "ok": True,
        "instruction": "Tell the caller warmly that you're connecting them to a team member now, in one short sentence.",
    }


async def request_callback(ctx: ToolContext, name: str | None, phone: str | None, message: str) -> dict:
    callback_phone = normalize_e164(phone) if phone else ctx.caller_phone
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        convo = await db.get(Conversation, ctx.conversation_id)
        convo.status = "escalated"
        db.add(Handoff(
            business_id=business.id, conversation_id=ctx.conversation_id, reason=message,
            outcome="callback_requested", callback_name=name, callback_phone=callback_phone, message=message,
        ))
        await db.commit()

    ctx.state["escalated"] = True
    if name:
        ctx.state.setdefault("customer_name", name)
    await ctx.notifier.callback_requested(business, name, callback_phone, message)
    await ctx.notifier.callback_received(business, callback_phone)
    ctx.bus.publish(ctx.business_id, {"type": "callback.requested", "conversation_id": ctx.conversation_id})
    return {"ok": True, "instruction": "Confirm a team member will call them back shortly, then ask if there's anything else."}
