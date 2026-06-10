"""Initial schema — users, audit_log, jobs, plugins.

Revision ID: 0001
Revises:
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(256), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(256), nullable=False),
        sa.Column(
            "role",
            sa.Enum("admin", "operator", "readonly", name="role"),
            nullable=False,
            server_default="readonly",
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("totp_secret", sa.String(64), nullable=True),
        sa.Column("totp_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"])

    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_personal_access_tokens_token_hash", "personal_access_tokens", ["token_hash"]
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource", sa.String(256), nullable=True),
        sa.Column("old_value", sa.Text, nullable=True),
        sa.Column("new_value", sa.Text, nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("result", sa.String(16), nullable=False, server_default="ok"),
    )
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("plugin_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "success", "failed", name="jobstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_jobs_plugin_id", "jobs", ["plugin_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "plugins",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("plugin_id", sa.String(64), nullable=False, unique=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_plugins_plugin_id", "plugins", ["plugin_id"])


def downgrade() -> None:
    op.drop_table("plugins")
    op.drop_table("jobs")
    op.drop_table("audit_log")
    op.drop_table("personal_access_tokens")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS role")
    op.execute("DROP TYPE IF EXISTS jobstatus")
