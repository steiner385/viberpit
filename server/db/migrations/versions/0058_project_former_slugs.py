"""Keep a product's previous slugs so feature-doc links survive a rename.

Revision ID: 0058_project_former_slugs
Revises: 0057_materialize_retry_baseline

Nullable JSON: init_db reconcile adds this on SQLite and Postgres. Existing rows
stay NULL — no backfill is possible or needed, since the one historical rename
(the #1365 product rename, reported as #1881) already had its referring docs
corrected by hand.
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_project_former_slugs"
down_revision = "0057_materialize_retry_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "former_slugs" not in columns:
        op.add_column("projects", sa.Column("former_slugs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "former_slugs")
