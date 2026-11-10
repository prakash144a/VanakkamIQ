"""Initial schema (frozen: the tables exactly as first deployed on 2026-11-09).

Never change this file. Every later schema change gets its own migration.

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.Integer(), primary_key=True)


def _business_fk() -> sa.Column:
    return sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _index(table: str, column: str) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "businesses",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("vertical", sa.String(40), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("address", sa.String(300)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("hours", sa.JSON(), nullable=False),
        sa.Column("persona", sa.Text()),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("voice", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80)),
        sa.Column("voice_path", sa.String(20), nullable=False),
        sa.Column("handoff_phone", sa.String(20)),
        sa.Column("notify_phone", sa.String(20)),
        sa.Column("sms_from", sa.String(20)),
        sa.Column("offers_pickup", sa.Boolean(), nullable=False),
        sa.Column("offers_delivery", sa.Boolean(), nullable=False),
        sa.Column("tax_rate_bps", sa.Integer(), nullable=False),
        sa.Column("large_order_cents", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _ts("created_at"),
    )
    op.create_table(
        "platform_admins",
        _id(),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("name", sa.String(120)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        _ts("created_at"),
    )
    op.create_table(
        "phone_numbers",
        _id(),
        _business_fk(),
        sa.Column("e164", sa.String(20), nullable=False, unique=True),
        sa.Column("carrier", sa.String(20), nullable=False),
    )
    _index("phone_numbers", "business_id")
    op.create_table(
        "staff_users",
        _id(),
        _business_fk(),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("name", sa.String(120)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
    )
    _index("staff_users", "business_id")
    op.create_table(
        "menu_categories",
        _id(),
        _business_fk(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    _index("menu_categories", "business_id")
    op.create_table(
        "menu_items",
        _id(),
        _business_fk(),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("menu_categories.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    _index("menu_items", "business_id")
    op.create_table(
        "modifier_groups",
        _id(),
        sa.Column("menu_item_id", sa.Integer(), sa.ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("min_select", sa.Integer(), nullable=False),
        sa.Column("max_select", sa.Integer(), nullable=False),
    )
    _index("modifier_groups", "menu_item_id")
    op.create_table(
        "modifiers",
        _id(),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("modifier_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_delta_cents", sa.Integer(), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
    )
    _index("modifiers", "group_id")
    op.create_table(
        "faqs",
        _id(),
        _business_fk(),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
    )
    _index("faqs", "business_id")
    op.create_table(
        "customers",
        _id(),
        _business_fk(),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120)),
        sa.Column("preferred_language", sa.String(20)),
        sa.Column("notes", sa.Text()),
        sa.Column("order_count", sa.Integer(), nullable=False),
        sa.Column("last_order_id", sa.Integer()),
        _ts("last_call_at", nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("business_id", "phone"),
    )
    _index("customers", "business_id")
    op.create_table(
        "conversations",
        _id(),
        _business_fk(),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_call_id", sa.String(120)),
        sa.Column("caller_phone", sa.String(20)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text()),
        _ts("started_at"),
        _ts("ended_at", nullable=True),
    )
    _index("conversations", "business_id")
    _index("conversations", "provider_call_id")
    _index("conversations", "status")
    op.create_table(
        "conversation_events",
        _id(),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("tool_name", sa.String(80)),
        sa.Column("data", sa.JSON()),
        _ts("created_at"),
    )
    _index("conversation_events", "conversation_id")
    op.create_table(
        "orders",
        _id(),
        _business_fk(),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("number", sa.Integer()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("fulfillment", sa.String(20), nullable=False),
        sa.Column("customer_name", sa.String(120)),
        sa.Column("customer_phone", sa.String(20)),
        sa.Column("delivery_address", sa.String(300)),
        sa.Column("requested_time", sa.String(60)),
        sa.Column("notes", sa.Text()),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("tax_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        _ts("created_at"),
        _ts("confirmed_at", nullable=True),
        sa.UniqueConstraint("business_id", "number"),
    )
    _index("orders", "business_id")
    _index("orders", "status")
    op.create_table(
        "order_items",
        _id(),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("menu_item_id", sa.Integer(), sa.ForeignKey("menu_items.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("modifiers", sa.JSON(), nullable=False),
        sa.Column("special_instructions", sa.Text()),
        sa.Column("line_total_cents", sa.Integer(), nullable=False),
    )
    _index("order_items", "order_id")
    op.create_table(
        "handoffs",
        _id(),
        _business_fk(),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("target", sa.String(60)),
        sa.Column("callback_name", sa.String(120)),
        sa.Column("callback_phone", sa.String(20)),
        sa.Column("message", sa.Text()),
        _ts("created_at"),
    )
    _index("handoffs", "business_id")
    _index("handoffs", "conversation_id")


def downgrade() -> None:
    for table in (
        "handoffs", "order_items", "orders", "conversation_events", "conversations", "customers", "faqs",
        "modifiers", "modifier_groups", "menu_items", "menu_categories", "staff_users", "phone_numbers",
        "platform_admins", "businesses",
    ):
        op.drop_table(table)
