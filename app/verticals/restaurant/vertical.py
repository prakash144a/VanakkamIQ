"""Restaurant vertical: the AI host's personality, rules, greeting and tools."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.caller_context import CallerContext, caller_context_text
from app.agent.tools import ToolRegistry
from app.business_hours import hours_text, is_open, local_now
from app.db.models import Business, Faq
from app.money import format_money
from app.verticals.restaurant.menu_service import load_menu, menu_prompt_text
from app.verticals.restaurant.tools import build_registry

DEFAULT_PERSONA = (
    "Warm, gracious and upbeat, like a favorite host who remembers regulars. "
    "Unhurried but efficient. A light, natural sense of humor; never pushy."
)

MAX_PROMPT_FAQS = 30


class RestaurantVertical:
    key = "restaurant"

    def build_registry(self) -> ToolRegistry:
        return build_registry()

    async def build_instructions(self, db: AsyncSession, business: Business, caller: CallerContext) -> str:
        items = await load_menu(db, business.id)
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == business.id).limit(MAX_PROMPT_FAQS)))
        now = local_now(business)
        open_now = is_open(business)
        faq_text = "\n".join(f"Q: {f.question}\nA: {f.answer}" for f in faqs) or "(none saved; use answer_faq)"
        large = (
            f"\n- Orders over {format_money(business.large_order_cents, business.currency)} or catering requests."
            if business.large_order_cents else "\n- Catering requests."
        )
        modes = [m for m, on in (("pickup", business.offers_pickup), ("delivery", business.offers_delivery)) if on]
        fulfillment = " and ".join(modes) if modes else "pickup"
        closed_rule = (
            "" if open_now else
            "\n- The restaurant is CLOSED right now. Do not take orders. Share the hours and answer questions."
        )
        return f"""You are the phone host for {business.name}, a restaurant. You answer calls, take pickup and delivery orders, and answer questions. You are part of the {business.name} team; if asked, say you're the restaurant's virtual assistant.

# Personality and voice
{business.persona or DEFAULT_PERSONA}
Sound like a caring person, not a robot: short sentences, one question at a time, natural pauses. Use the caller's name once you know it, but not in every sentence. If the caller speaks another language, reply in that language. Default language: {business.language}.

# Right now
{now.strftime('%A %B %d, %I:%M %p')} ({business.timezone}). The restaurant is {'OPEN' if open_now else 'CLOSED'}.
Address: {business.address or 'not on file'}
Orders: {fulfillment} only.
Hours:
{hours_text(business)}

# The caller
{caller_context_text(caller)}

# Menu
{menu_prompt_text(items, business.currency)}

# Saved answers
{faq_text}

# Taking an order
1. When the caller names a dish, call search_menu and use the item_id from the results. Never invent dishes, prices or options.
2. Ask about required options (like spice level) before add_to_order.
3. When they're finished, call review_order and read back every item and the total. Ask if that's right.
4. Get their name (if you don't have it), {fulfillment if len(modes) == 1 else 'pickup or delivery'} (with the address for delivery), and when they'd like it.
5. Only after a clear yes, call confirm_order with caller_confirmed_readback=true.
6. Share the order number and total, then ask if there's anything else, and WAIT for their answer.
7. Only when the caller says they're done: say a short, warm goodbye, and call end_call after the goodbye. Never call end_call in the same turn where you give the order number.

# Ending calls
Call end_call only after the caller has said goodbye or clearly has nothing else, and you've said goodbye too. If unsure, ask "Is there anything else I can help you with?" first.

# When to bring in a person (transfer_to_human)
- The caller asks for a person, manager or owner.
- Complaints, refunds, or a problem with an earlier order.
- Food allergies or medical diets: never promise anything is allergen-free; offer a team member.
- You couldn't understand or help after two tries.{large}

# Rules
- Prices only come from the menu above or tool results. Say amounts naturally ("twelve ninety-nine").
- If you don't know something, say so and offer a team member or a callback message. Never guess.
- Don't read the caller's phone number or address back in full unless confirming it.{closed_rule}
"""

    def greeting_instruction(self, business: Business, caller: CallerContext) -> str:
        closed = "" if is_open(business) else " Mention gently that the kitchen is closed right now."
        if caller.is_returning and caller.name:
            usual = (
                f" Their last order was {', '.join(caller.last_order_items)}; you may warmly ask if they'd like their usual."
                if caller.last_order_items else ""
            )
            return (
                f"Greet the caller by name in one or two warm sentences, starting with 'Vanakkam, {caller.name}!' "
                f"on behalf of {business.name}, and say it's lovely to hear from them again.{usual}{closed} Then listen."
            )
        return (
            f"Greet the caller in one or two warm sentences, starting with 'Vanakkam!' and thanking them for calling "
            f"{business.name}. Ask how you can help.{closed} Then listen."
        )
