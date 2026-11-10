"""Usage across businesses for the platform admin: calls, minutes, orders, rough AI cost."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.admin.common import as_utc
from app.db.models import Business, Conversation, Handoff, Order


@dataclass
class Usage:
    business: Business
    calls: int = 0
    seconds: int = 0
    orders: int = 0
    order_cents: int = 0
    messages: int = 0
    escalated: int = 0

    @property
    def minutes(self) -> float:
        return self.seconds / 60

    def ai_cost_cents(self, cents_per_minute: int) -> int:
        return round(self.minutes * cents_per_minute)


async def usage_since(db, days: int) -> list[Usage]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    businesses = list(await db.scalars(select(Business).order_by(Business.name)))
    usage = {b.id: Usage(b) for b in businesses}
    rows = await db.execute(select(Conversation.business_id, Conversation.started_at, Conversation.ended_at,
                                   Conversation.status).where(Conversation.started_at >= since))
    for business_id, started, ended, status in rows:
        u = usage.get(business_id)
        if u is None:
            continue
        u.calls += 1
        if ended:
            u.seconds += max(0, int((as_utc(ended) - as_utc(started)).total_seconds()))
        if status == "escalated":
            u.escalated += 1
    orders = await db.execute(select(Order.business_id, Order.total_cents).where(
        Order.confirmed_at >= since, Order.status != "cancelled", Order.status != "draft"))
    for business_id, total in orders:
        if business_id in usage:
            usage[business_id].orders += 1
            usage[business_id].order_cents += total
    messages = await db.scalars(select(Handoff.business_id).where(
        Handoff.created_at >= since, Handoff.outcome == "callback_requested"))
    for business_id in messages:
        if business_id in usage:
            usage[business_id].messages += 1
    return list(usage.values())
