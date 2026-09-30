"""app_settings.quiet_hours_timezone: the IANA zone quiet_hours_start/end are local to (#2562).

Record only — the column is nullable, so `_reconcile_columns` ALTER-ADDs it on both
dialects at startup; nothing runs `alembic upgrade` at deploy time. NULL means "unset →
UTC", the documented fallback until an operator picks a zone in Settings.
"""
import sqlalchemy as sa
from alembic import op

revision = "0075_quiet_hours_timezone"
down_revision = "0074_brand_control_band_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("quiet_hours_timezone", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "quiet_hours_timezone")
