"""Who is calling? Look up a returning customer so the AI can greet them personally."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, Order


@dataclass
class CallerContext:
    phone: str | None
    customer_id: int | None = None
    name: str | None = None
    display_name: str | None = None  # from caller ID, unverified
    order_count: int = 0
    last_order_items: list[str] = field(default_factory=list)
    preferred_language: str | None = None
    notes: str | None = None

    @property
    def is_returning(self) -> bool:
        return self.customer_id is not None and self.order_count > 0


async def load_caller_context(
    db: AsyncSession, business_id: int, phone: str | None, display_name: str | None = None
) -> CallerContext:
    ctx = CallerContext(phone=phone, display_name=display_name)
    if not phone:
        return ctx
    customer = await db.scalar(select(Customer).where(Customer.business_id == business_id, Customer.phone == phone))
    if customer is None:
        return ctx
    ctx.customer_id = customer.id
    ctx.name = customer.name
    ctx.order_count = customer.order_count
    ctx.preferred_language = customer.preferred_language
    ctx.notes = customer.notes
    if customer.last_order_id:
        order = await db.get(Order, customer.last_order_id)
        if order is not None:
            ctx.last_order_items = [f"{i.quantity} x {i.name}" for i in order.items]
    return ctx


def caller_context_text(ctx: CallerContext) -> str:
    if ctx.customer_id is None:
        hint = f' Caller ID shows the name "{ctx.display_name}" (unverified).' if ctx.display_name else ""
        return f"This appears to be a new caller.{hint} Ask for their name when you take the order."
    parts = [f"Returning customer: {ctx.name or 'name not on file'}."]
    if ctx.order_count:
        parts.append(f"They have ordered {ctx.order_count} time(s) before.")
    if ctx.last_order_items:
        parts.append("Their last order was: " + ", ".join(ctx.last_order_items) + ".")
    if ctx.preferred_language:
        parts.append(f"Preferred language: {ctx.preferred_language}.")
    if ctx.notes:
        parts.append(f"Staff notes: {ctx.notes}")
    return " ".join(parts)
