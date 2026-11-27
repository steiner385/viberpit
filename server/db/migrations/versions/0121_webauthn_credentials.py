"""webauthn_credentials + passkey session columns (#1410, G12).

Revision ID: 0121_webauthn_credentials
Revises: 0120_auto_resume_opt_out

A new table (``create_all`` installs it whole on both dialects) plus three nullable
``app_sessions`` columns (auto-reconciled); this revision is the reviewable record.
Written by ``server/auth/webauthn.py`` only.
"""
import sqlalchemy as sa
from alembic import op

revision = "0121_webauthn_credentials"
down_revision = "0120_auto_resume_opt_out"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("identity", sa.String(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=False, unique=True),
        sa.Column("public_key", sa.String(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transports", sa.JSON(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_webauthn_credentials_identity", "webauthn_credentials", ["identity"])
    op.add_column("app_sessions", sa.Column("auth_method", sa.String(), nullable=True))
    op.add_column("app_sessions", sa.Column("credential_id", sa.String(), nullable=True))
    op.add_column("app_sessions", sa.Column("last_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_sessions", "last_seen_at")
    op.drop_column("app_sessions", "credential_id")
    op.drop_column("app_sessions", "auth_method")
    op.drop_index("ix_webauthn_credentials_identity", table_name="webauthn_credentials")
    op.drop_table("webauthn_credentials")
