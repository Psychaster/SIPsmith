"""Add users.must_change_password — used by the first-run / forced-reset flow.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Default true so existing accounts get forced through a reset on next login —
    # this matches the security intent ("force a password reset on first login").
    # Operators can opt a specific user out by toggling the flag directly.
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
