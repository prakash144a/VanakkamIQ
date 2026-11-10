"""Any business type: ordering as a switchable module, owner dashboard, order status links,
and passwordless sign-in (emailed one-time codes).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("description", sa.Text()))
        batch.add_column(sa.Column("closures", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("greeting", sa.Text()))
        batch.add_column(sa.Column("takes_orders", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("transcript_retention_days", sa.Integer()))
    with op.batch_alter_table("staff_users") as batch:
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True)))
        batch.drop_column("password_hash")
    with op.batch_alter_table("platform_admins") as batch:
        batch.drop_column("password_hash")
    op.create_table(
        "otp_challenges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("audience", sa.String(20), nullable=False),
        sa.Column("code_sha256", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", "audience"),
    )
    with op.batch_alter_table("menu_items") as batch:
        batch.add_column(sa.Column("external_id", sa.String(80)))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("status_updated_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("public_token", sa.String(40)))
    op.create_index("ix_orders_public_token", "orders", ["public_token"], unique=True)
    with op.batch_alter_table("handoffs") as batch:
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("resolved_by", sa.String(254)))
    op.execute("UPDATE orders SET status = 'in_progress' WHERE status = 'in_kitchen'")


def downgrade() -> None:
    op.execute("UPDATE orders SET status = 'in_kitchen' WHERE status = 'in_progress'")
    with op.batch_alter_table("handoffs") as batch:
        batch.drop_column("resolved_by")
        batch.drop_column("resolved_at")
    op.drop_index("ix_orders_public_token", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("public_token")
        batch.drop_column("status_updated_at")
    with op.batch_alter_table("menu_items") as batch:
        batch.drop_column("external_id")
    op.drop_table("otp_challenges")
    # Passwords are gone for good; restored columns hold a placeholder nobody can sign in with.
    with op.batch_alter_table("platform_admins") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(255), nullable=False, server_default="!"))
    with op.batch_alter_table("staff_users") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(255), nullable=False, server_default="!"))
        batch.drop_column("last_login_at")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("transcript_retention_days")
        batch.drop_column("takes_orders")
        batch.drop_column("greeting")
        batch.drop_column("closures")
        batch.drop_column("description")
