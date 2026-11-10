"""Demo requests from the website (milestone 5b).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("business_name", sa.String(200), nullable=False),
        sa.Column("business_type", sa.String(40)),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(40)),
        sa.Column("message", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("email_sent", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_demo_requests_status", "demo_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_demo_requests_status", table_name="demo_requests")
    op.drop_table("demo_requests")
