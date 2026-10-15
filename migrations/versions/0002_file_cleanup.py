"""Durable original-file cleanup independent of document/collection cascades.

Revision ID: 0002_file_cleanup
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_file_cleanup"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("file_cleanup_tasks",
        sa.Column("storage_key", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.CheckConstraint("attempts >= 0", name="ck_file_cleanup_attempts"),
        sa.PrimaryKeyConstraint("storage_key"))
    op.create_index("ix_file_cleanup_claim", "file_cleanup_tasks", ["available_at", "created_at"])


def downgrade():
    op.drop_table("file_cleanup_tasks")
