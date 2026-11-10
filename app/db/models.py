"""Database models. Every business-owned table carries business_id (multi-tenant).

Some table and column names predate support for every business type (menu_items, vertical);
they are kept so existing databases need no renames. The Python names are the general ones.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    # restaurant | retail | services | other: wording and defaults only (see app/business_types.py)
    business_type: Mapped[str] = mapped_column("vertical", String(40), default="restaurant")
    description: Mapped[str | None] = mapped_column(Text)  # what the business does, in its own words
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    address: Mapped[str | None] = mapped_column(String(300))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # {"mon": [["11:00", "22:00"]], "tue": [...], ...}; missing day = closed
    hours: Mapped[dict] = mapped_column(default=dict)
    # [{"date": "2026-12-25", "note": "Christmas Day"}]: closed all day
    closures: Mapped[list] = mapped_column(default=list, server_default="[]")
    persona: Mapped[str | None] = mapped_column(Text)  # tone / personality notes for the AI
    greeting: Mapped[str | None] = mapped_column(Text)  # optional custom opening line
    language: Mapped[str] = mapped_column(String(20), default="en-US")
    voice: Mapped[str] = mapped_column(String(40), default="marin")
    model: Mapped[str | None] = mapped_column(String(80))  # per-business model override
    voice_path: Mapped[str] = mapped_column(String(20), default="openai_sip")  # later: media_stream
    handoff_phone: Mapped[str | None] = mapped_column(String(20))  # staff number for live transfer
    notify_phone: Mapped[str | None] = mapped_column(String(20))  # staff number for SMS alerts
    sms_from: Mapped[str | None] = mapped_column(String(20))  # Telnyx number SMS is sent from
    # Ordering module: catalog, cart, order confirmation, orders board
    takes_orders: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    offers_pickup: Mapped[bool] = mapped_column(Boolean, default=True)
    offers_delivery: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, default=0)  # 887 = 8.87%
    large_order_cents: Mapped[int | None] = mapped_column(Integer)  # above this, involve staff
    transcript_retention_days: Mapped[int | None] = mapped_column(Integer)  # None = keep
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformAdmin(Base):
    """VanakkamIQ operators: can see and onboard every business."""

    __tablename__ = "platform_admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OtpChallenge(Base):
    """A pending sign-in code. Only its SHA-256 hash is stored; the code itself exists only
    in the email. One per (email, audience); a new request replaces the old one."""

    __tablename__ = "otp_challenges"
    __table_args__ = (UniqueConstraint("email", "audience"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254))
    audience: Mapped[str] = mapped_column(String(20))  # platform | staff
    code_sha256: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    e164: Mapped[str] = mapped_column(String(20), unique=True)
    carrier: Mapped[str] = mapped_column(String(20), default="telnyx")


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner | staff
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogCategory(Base):
    """A section of the menu / product list / service list."""

    __tablename__ = "menu_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class CatalogItem(Base):
    """Something callers can order: a dish, a product, a service."""

    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("menu_categories.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(80))  # the business's own code (POS number, SKU)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    aliases: Mapped[list] = mapped_column(default=list)  # other ways callers say it
    tags: Mapped[list] = mapped_column(default=list)  # e.g. ["vegetarian", "spicy"]
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped[CatalogCategory | None] = relationship(lazy="selectin")
    modifier_groups: Mapped[list["ModifierGroup"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="ModifierGroup.id"
    )


class ModifierGroup(Base):
    __tablename__ = "modifier_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))  # e.g. "Spice level"
    min_select: Mapped[int] = mapped_column(Integer, default=0)  # 1 = required
    max_select: Mapped[int] = mapped_column(Integer, default=1)

    modifiers: Mapped[list["Modifier"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="Modifier.id"
    )


class Modifier(Base):
    __tablename__ = "modifiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("modifier_groups.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    price_delta_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)


class Faq(Base):
    __tablename__ = "faqs"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    phone: Mapped[str] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(120))
    preferred_language: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    last_order_id: Mapped[int | None] = mapped_column(Integer)
    last_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(20), default="voice")
    provider: Mapped[str] = mapped_column(String(40), default="openai_realtime")
    provider_call_id: Mapped[str | None] = mapped_column(String(120), index=True)
    caller_phone: Mapped[str | None] = mapped_column(String(20))
    # active | completed | escalated | abandoned | failed
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["ConversationEvent"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="ConversationEvent.id"
    )


class ConversationEvent(Base):
    __tablename__ = "conversation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # caller | agent | tool | system
    text: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(String(80))
    data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("business_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    number: Mapped[int | None] = mapped_column(Integer)  # assigned on confirm
    # draft | confirmed | in_progress | ready | completed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Unguessable id for the caller's order-status link
    public_token: Mapped[str | None] = mapped_column(String(40), index=True, unique=True)
    fulfillment: Mapped[str] = mapped_column(String(20), default="pickup")  # pickup | delivery
    customer_name: Mapped[str | None] = mapped_column(String(120))
    customer_phone: Mapped[str | None] = mapped_column(String(20))
    delivery_address: Mapped[str | None] = mapped_column(String(300))
    requested_time: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["OrderItem"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="OrderItem.id"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[int | None] = mapped_column(ForeignKey("menu_items.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(160))  # snapshot at order time
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_cents: Mapped[int] = mapped_column(Integer)  # base + modifiers
    modifiers: Mapped[list] = mapped_column(default=list)  # [{"name", "price_delta_cents"}]
    special_instructions: Mapped[str | None] = mapped_column(Text)
    line_total_cents: Mapped[int] = mapped_column(Integer)


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    # transferring | callback_requested | failed
    outcome: Mapped[str] = mapped_column(String(30))
    target: Mapped[str | None] = mapped_column(String(60))
    callback_name: Mapped[str | None] = mapped_column(String(120))
    callback_phone: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # staff marked it done
    resolved_by: Mapped[str | None] = mapped_column(String(254))
