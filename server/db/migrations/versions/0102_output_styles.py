"""Output styles (D-16a): sessions.output_style and app_settings.default_output_style.

Revision ID: 0102_output_styles
Revises: 0101_mcp_grant_health

``sessions.output_style`` is the session's chosen CLI output style, re-applied through
the spawn ``--settings`` JSON on every resume and respawn; NULL means "use the app
default". ``app_settings.default_output_style`` is that default; NULL means the CLI's
own ``default``. ``sessions.initial_command_identity`` is the person who typed an app
command (`/plan`, `/fast` …) as the initial prompt — the proof a rehydrate needs to
re-park it. All nullable: ``init_db``'s reconcile adds them on both dialects; this
revision is the reviewable record.

Chaining note: chained off 0101_mcp_grant_health (master's newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0102_output_styles"
down_revision = "0101_mcp_grant_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("output_style", sa.String(), nullable=True))
    op.add_column("app_settings",
                  sa.Column("default_output_style", sa.String(), nullable=True))
    op.add_column("sessions",
                  sa.Column("initial_command_identity", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "initial_command_identity")
    op.drop_column("app_settings", "default_output_style")
    op.drop_column("sessions", "output_style")
