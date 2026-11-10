"""Builds the AI for one call from the business's type and switched-on modules.

Every business: answers questions (saved answers, hours, closures), takes messages, transfers
to a person, recognizes returning callers. With `takes_orders`: the catalog and ordering tools.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.caller_context import CallerContext, caller_context_text
from app.agent.tools import ToolRegistry
from app.business_hours import closures_text, hours_text, is_open, local_now
from app.business_types import BusinessType, get_type
from app.capabilities.core import core_tools
from app.capabilities.ordering.catalog import catalog_prompt_text, load_catalog
from app.capabilities.ordering.tools import ordering_tools
from app.db.models import Business, Faq
from app.money import format_money

MAX_PROMPT_FAQS = 30


@dataclass
class Assistant:
    registry: ToolRegistry
    instructions: str
    greeting: str


async def build_assistant(db: AsyncSession, business: Business, caller: CallerContext) -> Assistant:
    btype = get_type(business.business_type)
    tools = core_tools()
    if business.takes_orders:
        tools = ordering_tools(business) + tools
    return Assistant(
        registry=ToolRegistry(tools),
        instructions=await build_instructions(db, business, btype, caller),
        greeting=greeting_instruction(business, btype, caller),
    )


def _fulfillment(business: Business) -> tuple[list[str], str]:
    modes = [m for m, on in (("pickup", business.offers_pickup), ("delivery", business.offers_delivery)) if on] or ["pickup"]
    return modes, " and ".join(modes)


async def _ordering_section(db: AsyncSession, business: Business, btype: BusinessType, open_now: bool) -> str:
    items = await load_catalog(db, business.id)
    modes, fulfillment = _fulfillment(business)
    if not items:
        return (f"# Orders\nThe {btype.catalog.lower()} isn't set up yet, so you can't take orders. "
                "Offer to take a message for the team instead.")
    closed = "" if open_now else "\nWe are CLOSED right now: do not take orders. Share the hours instead.\n"
    return f"""# {btype.catalog}
{catalog_prompt_text(items, business.currency)}

# Taking an order ({fulfillment} only)
{closed}1. When the caller names a {btype.item}, call search_catalog and use the item_id from the results. Never invent items, prices or options.
2. Ask about required options before add_to_order.
3. When they're finished, call review_order and read back every item and the total. Ask if that's right.
4. Get their name (if you don't have it), {fulfillment if len(modes) == 1 else 'pickup or delivery'} (with the address for delivery), and when they'd like it.
5. Only after a clear yes, call confirm_order with caller_confirmed_readback=true.
6. Share the order number and total, then ask if there's anything else, and WAIT for their answer.
7. Never call end_call in the same turn where you give the order number."""


async def build_instructions(db: AsyncSession, business: Business, btype: BusinessType, caller: CallerContext) -> str:
    faqs = list(await db.scalars(select(Faq).where(Faq.business_id == business.id).limit(MAX_PROMPT_FAQS)))
    now = local_now(business)
    open_now = is_open(business)
    faq_text = "\n".join(f"Q: {f.question}\nA: {f.answer}" for f in faqs) or "(none saved; use answer_faq)"
    closures = closures_text(business)

    can_do = ["answer questions about the business"]
    if business.takes_orders:
        can_do.append(f"take {_fulfillment(business)[1]} orders")
    can_do.append("take a message so the team can call back")
    handoff_rules = [
        "The caller asks for a person, manager or owner.",
        "Complaints, refunds, or a problem with an earlier order or visit.",
        "You couldn't understand or help after two tries.",
        *btype.handoff_rules,
    ]
    if business.takes_orders and business.large_order_cents:
        handoff_rules.append(f"Orders over {format_money(business.large_order_cents, business.currency)}.")

    sections = [
        f"""You are the {btype.role} for {business.name}, {btype.kind}. You answer the phone and {', '.join(can_do[:-1])} and {can_do[-1]}. You are part of the {business.name} team; if asked, say you're its virtual assistant.""",
        f"# About {business.name}\n{business.description}" if business.description else "",
        f"""# Personality and voice
{business.persona or btype.persona}
Sound like a caring person, not a robot: short sentences, one question at a time, natural pauses. Use the caller's name once you know it, but not in every sentence. If the caller speaks another language, reply in that language. Default language: {business.language}.""",
        f"""# Right now
{now.strftime('%A %B %d, %I:%M %p')} ({business.timezone}). We are {'OPEN' if open_now else 'CLOSED'}.
Address: {business.address or 'not on file'}
Hours:
{hours_text(business)}""" + (f"\nUpcoming closures:\n{closures}" if closures else ""),
        f"# The caller\n{caller_context_text(caller)}",
        await _ordering_section(db, business, btype, open_now) if business.takes_orders else "",
        f"# Saved answers\n{faq_text}",
        """# Taking a message
When the caller wants someone to call back, or nobody can take a transfer: get their name, confirm the best callback number (they may use the number they're calling from), and what it's about. Then call request_callback.""",
        """# Ending calls
Call end_call only after the caller has said goodbye or clearly has nothing else, and you've said goodbye too. If unsure, ask "Is there anything else I can help you with?" first.""",
        "# When to bring in a person (transfer_to_human)\n" + "\n".join(f"- {rule}" for rule in handoff_rules),
        """# Rules
- Facts, prices and policies only come from this page or tool results. If you don't know something, say so and offer a team member or a message. Never guess.
- Say amounts naturally ("twelve ninety-nine").
- Don't read the caller's phone number or address back in full unless confirming it.""",
    ]
    return "\n\n".join(s for s in sections if s) + "\n"


def greeting_instruction(business: Business, btype: BusinessType, caller: CallerContext) -> str:
    closed = "" if is_open(business) else f" Mention gently that {btype.closed_note}."
    if business.greeting:
        opener = f"Open with this greeting, said naturally: \"{business.greeting}\""
    else:
        opener = f"Greet the caller in one or two warm sentences, starting with 'Vanakkam!' and thanking them for calling {business.name}. Ask how you can help."
    if caller.is_returning and caller.name:
        usual = (
            f" Their last order was {', '.join(caller.last_order_items)}; you may warmly ask if they'd like the same again."
            if business.takes_orders and caller.last_order_items else ""
        )
        return (
            f"Greet the caller by name in one or two warm sentences, starting with 'Vanakkam, {caller.name}!' "
            f"on behalf of {business.name}, and say it's lovely to hear from them again.{usual}{closed} Then listen."
        )
    return f"{opener}{closed} Then listen."
