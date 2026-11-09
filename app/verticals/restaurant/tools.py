"""Tools the AI uses during a restaurant call."""

from sqlalchemy import select

from app.agent.tools import Tool, ToolContext, ToolRegistry, schema
from app.business_hours import is_open
from app.db.models import Business, Faq, MenuItem
from app.handoff import service as handoff
from app.money import format_money
from app.verticals.restaurant import order_service as orders
from app.verticals.restaurant.menu_service import item_view, load_menu, resolve_modifiers, search_faqs, search_items

MAX_QUANTITY = 50


async def search_menu(ctx: ToolContext, args: dict) -> dict:
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        items = await load_menu(db, ctx.business_id)
    matches = search_items(items, args.get("query", ""))
    if not matches:
        return {"ok": True, "matches": [], "note": "Nothing on the menu matches. Say so and suggest close alternatives."}
    return {"ok": True, "matches": [item_view(item, business.currency) for item, _ in matches]}


async def add_to_order(ctx: ToolContext, args: dict) -> dict:
    quantity = int(args.get("quantity") or 1)
    if not 1 <= quantity <= MAX_QUANTITY:
        return {"ok": False, "error": f"Quantity must be between 1 and {MAX_QUANTITY}."}
    async with ctx.session_factory() as db:
        business = await db.get(Business, ctx.business_id)
        if not is_open(business):
            return {"ok": False, "error": "The restaurant is closed, so orders can't be taken now. Share the opening hours."}
        item = await db.get(MenuItem, int(args["item_id"]))
        if item is None or item.business_id != ctx.business_id:
            return {"ok": False, "error": "Unknown item_id. Use search_menu first."}
        if not item.is_available:
            return {"ok": False, "error": f"{item.name} is sold out today. Suggest something similar."}
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
            return {"ok": False, "error": "This restaurant doesn't deliver. Offer pickup instead."}
        if fulfillment == "pickup" and not business.offers_pickup:
            return {"ok": False, "error": "This restaurant only does delivery. Ask for the delivery address."}
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
    text_note = " and that a text confirmation is on its way" if order.customer_phone else ""
    return {
        "ok": True,
        "order_number": order.number,
        "total": format_money(order.total_cents, business.currency),
        "instruction": f"Tell the caller their order number and total{text_note}. Then ask if there's anything else.",
    }


async def answer_faq(ctx: ToolContext, args: dict) -> dict:
    async with ctx.session_factory() as db:
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == ctx.business_id)))
    found = search_faqs(faqs, args.get("question", ""))
    if not found:
        return {"ok": True, "answers": [], "note": "No saved answer. Don't guess: offer to connect them with a team member or take a message."}
    return {"ok": True, "answers": [{"question": f.question, "answer": f.answer} for f in found]}


async def transfer_to_human(ctx: ToolContext, args: dict) -> dict:
    return await handoff.start_transfer(ctx, args.get("reason") or "Caller needs a team member")


async def request_callback(ctx: ToolContext, args: dict) -> dict:
    return await handoff.request_callback(ctx, args.get("name"), args.get("phone"), args.get("message") or "")


async def end_call(ctx: ToolContext, args: dict) -> dict:
    ctx.call.request_hangup()
    return {"ok": True, "instruction": "Say a short, warm goodbye now. The call will end right after."}


def build_registry() -> ToolRegistry:
    return ToolRegistry([
        Tool("search_menu", "Find menu items by what the caller said. Use before adding any item.",
             schema({"query": {"type": "string", "description": "Dish name as the caller said it"}}, ["query"]),
             search_menu),
        Tool("add_to_order", "Add a menu item to the caller's order.",
             schema({
                 "item_id": {"type": "integer", "description": "item_id from search_menu"},
                 "quantity": {"type": "integer", "minimum": 1},
                 "options": {"type": "array", "items": {"type": "string"}, "description": "Chosen option names, e.g. spice level"},
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
                 "fulfillment": {"type": "string", "enum": ["pickup", "delivery"]},
                 "delivery_address": {"type": "string"},
                 "requested_time": {"type": "string", "description": "e.g. 'ASAP' or '7:15 PM'"},
                 "notes": {"type": "string"},
                 "caller_confirmed_readback": {"type": "boolean", "description": "True only after the caller said yes to the read-back"},
             }, ["customer_name", "fulfillment", "caller_confirmed_readback"]),
             confirm_order),
        Tool("answer_faq", "Look up the restaurant's saved answer to a caller's question.",
             schema({"question": {"type": "string"}}, ["question"]), answer_faq),
        Tool("transfer_to_human", "Connect the caller with a team member (see the rules for when).",
             schema({"reason": {"type": "string"}}, ["reason"]), transfer_to_human),
        Tool("request_callback", "Leave a callback message for staff when nobody can take the call.",
             schema({
                 "name": {"type": "string"},
                 "phone": {"type": "string", "description": "Only if different from the number they're calling from"},
                 "message": {"type": "string"},
             }, ["message"]),
             request_callback),
        Tool("end_call", "End the call after the caller is done and you've said goodbye.", schema({}), end_call),
    ])
