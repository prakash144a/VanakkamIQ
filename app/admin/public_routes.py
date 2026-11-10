"""Caller-facing pages. No login: the order link holds an unguessable token."""

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.admin.common import db_session, local, templates
from app.business_types import get_type
from app.db.models import Business, Order, PhoneNumber

router = APIRouter()

STEPS = ("confirmed", "in_progress", "ready", "completed")


@router.get("/o/{token}")
async def order_status(request: Request, token: str):
    order = business = number = None
    if 16 <= len(token) <= 40:
        async with db_session(request) as db:
            order = await db.scalar(select(Order).where(Order.public_token == token))
            if order is not None:
                business = await db.get(Business, order.business_id)
                number = await db.scalar(select(PhoneNumber.e164).where(PhoneNumber.business_id == business.id))
    if order is None or business is None:
        response = templates.TemplateResponse(request, "public/order_missing.html", {}, status_code=404)
    else:
        btype = get_type(business.business_type)
        reached = STEPS.index(order.status) if order.status in STEPS else -1
        # TemplateResponse, not render(): no session cookie for callers.
        response = templates.TemplateResponse(request, "public/order_status.html", {
            "o": order, "b": business, "btype": btype, "steps": STEPS, "reached": reached, "phone": number,
            "placed": local(order.confirmed_at, business)})
    # Private to whoever has the link: never cached or indexed.
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
