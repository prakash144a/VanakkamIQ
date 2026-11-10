"""Orders: the cart during a call, confirmation, and the status flow staff move orders through."""

import secrets
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import ToolContext
from app.db.models import CatalogItem, Customer, Modifier, Order, OrderItem
from app.money import format_money


async def current_order(db: AsyncSession, ctx: ToolContext) -> Order | None:
    order_id = ctx.state.get("order_id")
    return await db.get(Order, order_id) if order_id else None


async def get_or_create_draft(db: AsyncSession, ctx: ToolContext) -> Order:
    order = await current_order(db, ctx)
    if order is not None and order.status == "draft":
        return order
    order = Order(
        business_id=ctx.business_id, conversation_id=ctx.conversation_id,
        customer_id=ctx.customer_id, customer_phone=ctx.caller_phone, items=[],
    )
    db.add(order)
    await db.flush()
    ctx.state["order_id"] = order.id
    return order


def recalc(order: Order, tax_rate_bps: int) -> None:
    order.subtotal_cents = sum(line.line_total_cents for line in order.items)
    order.tax_cents = round(order.subtotal_cents * tax_rate_bps / 10_000)
    order.total_cents = order.subtotal_cents + order.tax_cents


def add_line(order: Order, item: CatalogItem, quantity: int, modifiers: list[Modifier], instructions: str | None) -> OrderItem:
    unit = item.price_cents + sum(m.price_delta_cents for m in modifiers)
    line = OrderItem(
        menu_item_id=item.id, name=item.name, quantity=quantity, unit_price_cents=unit,
        modifiers=[{"name": m.name, "price_delta_cents": m.price_delta_cents} for m in modifiers],
        special_instructions=instructions or None, line_total_cents=unit * quantity,
    )
    order.items.append(line)
    return line


def set_quantity(line: OrderItem, quantity: int) -> None:
    line.quantity = quantity
    line.line_total_cents = line.unit_price_cents * quantity


def order_view(order: Order, currency: str) -> dict:
    return {
        "status": order.status,
        "number": order.number,
        "lines": [
            {
                "line_id": line.id,
                "name": line.name,
                "quantity": line.quantity,
                **({"options": [m["name"] for m in line.modifiers]} if line.modifiers else {}),
                **({"special_instructions": line.special_instructions} if line.special_instructions else {}),
                "line_total": format_money(line.line_total_cents, currency),
            }
            for line in order.items
        ],
        "subtotal": format_money(order.subtotal_cents, currency),
        "tax": format_money(order.tax_cents, currency),
        "total": format_money(order.total_cents, currency),
        "fulfillment": order.fulfillment,
    }


async def next_order_number(db: AsyncSession, business_id: int) -> int:
    current = await db.scalar(select(func.max(Order.number)).where(Order.business_id == business_id))
    return (current or 1000) + 1


async def confirm(
    db: AsyncSession, order: Order, *, customer_name: str, fulfillment: str,
    delivery_address: str | None, requested_time: str | None, notes: str | None,
) -> Order:
    order.customer_name = customer_name.strip()
    order.fulfillment = fulfillment
    order.delivery_address = delivery_address or None
    order.requested_time = requested_time or None
    order.notes = notes or None
    order.number = await next_order_number(db, order.business_id)
    order.status = "confirmed"
    order.confirmed_at = order.status_updated_at = datetime.now(timezone.utc)
    order.public_token = secrets.token_urlsafe(16)

    if order.customer_phone:
        customer = await db.scalar(
            select(Customer).where(Customer.business_id == order.business_id, Customer.phone == order.customer_phone)
        )
        if customer is None:
            customer = Customer(business_id=order.business_id, phone=order.customer_phone, order_count=0)
            db.add(customer)
        if not customer.name:
            customer.name = order.customer_name
        customer.order_count = (customer.order_count or 0) + 1
        customer.last_order_id = order.id
        await db.flush()
        order.customer_id = customer.id
    return order


# --- status flow (orders board) -----------------------------------------------------------
# confirmed ("New") -> in_progress -> ready -> completed; cancelled from any open state.
NEXT_STATUS = {"confirmed": "in_progress", "in_progress": "ready", "ready": "completed"}
OPEN_STATUSES = ("confirmed", "in_progress", "ready")
STAFF_STATUSES = {*OPEN_STATUSES, "completed", "cancelled"}


class StatusChangeError(ValueError):
    pass


def change_status(order: Order, new_status: str) -> str:
    """Move an order to a new status; returns the previous one. Raises StatusChangeError."""
    if new_status not in STAFF_STATUSES:
        raise StatusChangeError(f"Unknown status {new_status!r}.")
    if order.status == "draft":
        raise StatusChangeError("This order was never confirmed.")
    if order.status in ("completed", "cancelled") and new_status != order.status:
        # Allow undoing a mistaken tap back to an open state.
        if new_status not in OPEN_STATUSES:
            raise StatusChangeError("This order is already closed.")
    previous = order.status
    order.status = new_status
    order.status_updated_at = datetime.now(timezone.utc)
    return previous
