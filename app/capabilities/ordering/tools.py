"""Ordering tools: find items, build the order, read it back, confirm it."""

from app.agent.tools import Tool, ToolContext, schema
from app.business_hours import is_open
from app.business_types import get_type
from app.capabilities.ordering import orders
from app.capabilities.ordering.catalog import item_view, load_catalog, resolve_modifiers, search_items
from app.db.models import Business, CatalogItem
from app.money import format_money

MAX_QUANTITY = 50


async def search_catalog(ctx: ToolContext, args: dict) -> dict:
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        items = await load_catalog(db, ctx.business_id)
    matches = search_items(items, args.get("query", ""))
    if not matches:
        return {"ok": True, "matches": [], "note": "Nothing matches. Say so and suggest close alternatives."}
    return {"ok": True, "matches": [item_view(item, business.currency) for item, _ in matches]}


async def add_to_order(ctx: ToolContext, args: dict) -> dict:
    quantity = int(args.get("quantity") or 1)
    if not 1 <= quantity <= MAX_QUANTITY:
        return {"ok": False, "error": f"Quantity must be between 1 and {MAX_QUANTITY}."}
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        if not is_open(business):
            return {"ok": False, "error": "We're closed, so orders can't be taken now. Share the opening hours."}
        item = await db.get(CatalogItem, int(args["item_id"]))
        if item is None or item.business_id != ctx.business_id:
            return {"ok": False, "error": "Unknown item_id. Use search_catalog first."}
        if not item.is_available:
            return {"ok": False, "error": f"{item.name} is unavailable today. Suggest something similar."}
        modifiers, problems = resolve_modifiers(item, args.get("options") or [])
        if problems:
            return {"ok": False, "needs": problems}
        order = await orders.get_or_create_draft(db, ctx)
        orders.add_line(order, item, quantity, modifiers, args.get("special_instructions"))
        orders.recalc(order, business.tax_rate_bps)
        await db.flush()
        view = orders.order_view(order, business.currency)
        large = business.large_order_cents and order.total_cents > business.large_order_cents
        await db.commit()
    result = {"ok": True, "added": f"{quantity} x {item.name}", "order": view}
    if large:
        result["note"] = "This is now a large order. After reviewing it, offer to connect the caller with a team member to confirm it (transfer_to_human)."
    return result


async def update_order_item(ctx: ToolContext, args: dict) -> dict:
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        order = await orders.current_order(db, ctx)
        if order is None or order.status != "draft":
            return {"ok": False, "error": "There is no open order."}
        line = next((li for li in order.items if li.id == int(args["line_id"])), None)
        if line is None:
            return {"ok": False, "error": "Unknown line_id. Call review_order to see the lines."}
        if "quantity" in args and args["quantity"] is not None:
            quantity = int(args["quantity"])
            if quantity <= 0:
                order.items.remove(line)
            elif quantity > MAX_QUANTITY:
                return {"ok": False, "error": f"Quantity must be at most {MAX_QUANTITY}."}
            else:
                orders.set_quantity(line, quantity)
        if args.get("special_instructions") is not None and line in order.items:
            line.special_instructions = args["special_instructions"] or None
        orders.recalc(order, business.tax_rate_bps)
        await db.flush()
        view = orders.order_view(order, business.currency)
        await db.commit()
    return {"ok": True, "order": view}


async def remove_from_order(ctx: ToolContext, args: dict) -> dict:
    return await update_order_item(ctx, {"line_id": args["line_id"], "quantity": 0})


async def review_order(ctx: ToolContext, args: dict) -> dict:
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        order = await orders.current_order(db, ctx)
        if order is None or not order.items:
            return {"ok": True, "order": None, "note": "The order is empty."}
        return {"ok": True, "order": orders.order_view(order, business.currency)}


async def confirm_order(ctx: ToolContext, args: dict) -> dict:
    if not args.get("caller_confirmed_readback"):
        return {"ok": False, "error": "First read back every item and the total, and get a clear yes from the caller."}
    fulfillment = args.get("fulfillment", "pickup")
    if fulfillment not in ("pickup", "delivery"):
        return {"ok": False, "error": "fulfillment must be pickup or delivery."}
    if fulfillment == "delivery" and not args.get("delivery_address"):
        return {"ok": False, "error": "Ask for the delivery address first."}
    name = (args.get("customer_name") or "").strip()
    if not name:
        return {"ok": False, "error": "Ask for the caller's name first."}

    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        if fulfillment == "delivery" and not business.offers_delivery:
            return {"ok": False, "error": "We don't deliver. Offer pickup instead."}
        if fulfillment == "pickup" and not business.offers_pickup:
            return {"ok": False, "error": "We only deliver. Ask for the delivery address."}
        order = await orders.current_order(db, ctx)
        if order is None or order.status != "draft" or not order.items:
            return {"ok": False, "error": "There is no open order to confirm."}
        await orders.confirm(
            db, order, customer_name=name, fulfillment=fulfillment,
            delivery_address=args.get("delivery_address"), requested_time=args.get("requested_time"),
            notes=args.get("notes"),
        )
        await db.commit()
        view = orders.order_view(order, business.currency)

    ctx.state["customer_name"] = name
    ctx.state["confirmed_order_id"] = order.id
    await ctx.notifier.order_confirmed(business, order)
    ctx.bus.publish(ctx.business_id, {
        "type": "order.confirmed", "order_id": order.id, "number": order.number,
        "conversation_id": ctx.conversation_id, "total": view["total"],
    })
    text_note = " and that a text with the order details and a status link is on its way" if order.customer_phone else ""
    return {
        "ok": True,
        "order_number": order.number,
        "total": format_money(order.total_cents, business.currency),
        "instruction": f"Tell the caller their order number and total{text_note}. Then ask if there's anything else.",
    }


def ordering_tools(business: Business) -> list[Tool]:
    noun = get_type(business.business_type).item
    modes = [m for m, on in (("pickup", business.offers_pickup), ("delivery", business.offers_delivery)) if on] or ["pickup"]
    return [
        Tool("search_catalog", f"Find a {noun} by what the caller said. Use before adding anything to the order.",
             schema({"query": {"type": "string", "description": f"The {noun} as the caller said it"}}, ["query"]),
             search_catalog),
        Tool("add_to_order", f"Add a {noun} to the caller's order.",
             schema({
                 "item_id": {"type": "integer", "description": "item_id from search_catalog"},
                 "quantity": {"type": "integer", "minimum": 1},
                 "options": {"type": "array", "items": {"type": "string"}, "description": "Chosen option names"},
                 "special_instructions": {"type": "string"},
             }, ["item_id", "quantity"]),
             add_to_order),
        Tool("update_order_item", "Change the quantity (0 removes it) or special instructions of an order line.",
             schema({
                 "line_id": {"type": "integer"},
                 "quantity": {"type": "integer", "minimum": 0},
                 "special_instructions": {"type": "string"},
             }, ["line_id"]),
             update_order_item),
        Tool("remove_from_order", "Remove a line from the order.",
             schema({"line_id": {"type": "integer"}}, ["line_id"]), remove_from_order),
        Tool("review_order", "Get the current order with line ids, prices and total.", schema({}), review_order),
        Tool("confirm_order", "Place the order after reading it back and getting the caller's yes.",
             schema({
                 "customer_name": {"type": "string"},
                 "fulfillment": {"type": "string", "enum": modes},
                 "delivery_address": {"type": "string"},
                 "requested_time": {"type": "string", "description": "e.g. 'ASAP' or '7:15 PM'"},
                 "notes": {"type": "string"},
                 "caller_confirmed_readback": {"type": "boolean", "description": "True only after the caller said yes to the read-back"},
             }, ["customer_name", "fulfillment", "caller_confirmed_readback"]),
             confirm_order),
    ]
