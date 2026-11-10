"""Per-business switch for live transfers to staff (off = the AI promises a call back),
and how each call was routed to its business (dialed number, which SIP header matched).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("transfers_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("dialed_number", sa.String(20)))
        batch.add_column(sa.Column("routed_by", sa.String(40)))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("routed_by")
        batch.drop_column("dialed_number")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("transfers_enabled")
