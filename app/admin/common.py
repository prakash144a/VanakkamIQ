"""Shared pieces of the admin screens: templates, rendering, time display, call queries."""

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import lazyload

from app.admin.auth import csrf_token
from app.business_hours import DAY_NAMES, DAYS
from app.business_types import TYPES, get_type, status_label
from app.db.models import Business, Conversation, Handoff, Order
from app.money import format_money

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(
    DAYS=DAYS, DAY_NAMES=DAY_NAMES, format_money=format_money, get_type=get_type, status_label=status_label,
    TYPES_JSON=json.dumps({k: {"catalog": t.catalog, "item": t.item, "takes_orders": t.takes_orders,
                               "catalog_example": t.catalog_example, "faq_example": t.faq_example}
                           for k, t in TYPES.items()}),
)


def render(request: Request, template: str, status_code: int = 200, **context):
    return templates.TemplateResponse(
        request, template, {"csrf": csrf_token(request), **context}, status_code=status_code
    )


def db_session(request: Request):
    return request.app.state.services.session_factory()


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite returns naive UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local(dt: datetime | None, business: Business) -> datetime | None:
    dt = as_utc(dt)
    return dt.astimezone(ZoneInfo(business.timezone)) if dt else None


def call_seconds(convo: Conversation) -> int | None:
    if not convo.ended_at:
        return None
    return max(0, int((as_utc(convo.ended_at) - as_utc(convo.started_at)).total_seconds()))


def duration(convo: Conversation) -> str:
    seconds = call_seconds(convo)
    if seconds is None:
        return "in progress" if convo.status == "active" else "—"
    return f"{seconds // 60}m {seconds % 60:02d}s"


async def recent_calls(db, business: Business, *, limit: int = 50, status: str | None = None) -> list[dict]:
    query = (select(Conversation).options(lazyload(Conversation.events))
             .where(Conversation.business_id == business.id))
    if status:
        query = query.where(Conversation.status == status)
    convos = list(await db.scalars(query.order_by(Conversation.started_at.desc()).limit(limit)))
    ids = [c.id for c in convos] or [0]
    orders = {o.conversation_id: o for o in await db.scalars(select(Order).where(
        Order.conversation_id.in_(ids), Order.status.not_in(("draft", "cancelled"))))}
    return [{"c": c, "order": orders.get(c.id), "when": local(c.started_at, business), "duration": duration(c)}
            for c in convos]


async def conversation_view(db, convo: Conversation, business: Business, *, back_href: str, back_label: str,
                            events_url: str | None = None) -> dict:
    orders = list(await db.scalars(select(Order).where(Order.conversation_id == convo.id)))
    handoffs = list(await db.scalars(select(Handoff).where(Handoff.conversation_id == convo.id)))
    events = [{"e": e, "when": local(e.created_at, business)} for e in convo.events]
    return {"c": convo, "b": business, "events": events, "orders": orders, "handoffs": handoffs,
            "when": local(convo.started_at, business), "duration": duration(convo),
            "back_href": back_href, "back_label": back_label, "events_url": events_url}
