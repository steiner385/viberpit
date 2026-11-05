"""Release milestones: milestone_ladders, milestones, milestone_measurements.

Revision ID: 0105_release_milestones
Revises: 0104_curated_marketplaces

``milestone_ladders`` caches one product's parsed milestone definition file (unique per
project); ``milestones`` are its rungs; ``milestone_measurements`` are the cached gate
readings, number readings and leaf lists, keyed by (ladder, scope, key). ``init_db``'s
``create_all`` creates these on both dialects; this revision is the reviewable record.

Chaining note: chained off 0104_curated_marketplaces (master's newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0105_release_milestones"
down_revision = "0104_curated_marketplaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "milestone_ladders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=False, unique=True),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("source_sha", sa.String(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("spec_path", sa.String(), nullable=True),
        sa.Column("definition", sa.JSON(), nullable=True),
        sa.Column("parse_error", sa.String(), nullable=True),
        sa.Column("parse_error_sha", sa.String(), nullable=True),
        sa.Column("refresh_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "milestones",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ladder_id", sa.String(), sa.ForeignKey("milestone_ladders.id"),
                  nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("audience", sa.String(), nullable=True),
        sa.Column("platforms", sa.JSON(), nullable=True),
        sa.Column("epics", sa.JSON(), nullable=True),
        sa.Column("refs", sa.JSON(), nullable=True),
        sa.Column("gate", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_override", sa.JSON(), nullable=True),
        sa.Column("status_note", sa.String(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ladder_id", "key", name="uq_milestone_key"),
    )
    op.create_index("ix_milestones_ladder_id", "milestones", ["ladder_id"])
    op.create_table(
        "milestone_measurements",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ladder_id", sa.String(), sa.ForeignKey("milestone_ladders.id"),
                  nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("target", sa.JSON(), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("manual_by", sa.String(), nullable=True),
        sa.Column("manual_evidence", sa.String(), nullable=True),
        sa.Column("manual_url", sa.String(), nullable=True),
        sa.UniqueConstraint("ladder_id", "scope", "key", name="uq_milestone_measurement"),
    )
    op.create_index("ix_milestone_measurements_ladder_id", "milestone_measurements",
                    ["ladder_id"])


def downgrade() -> None:
    op.drop_index("ix_milestone_measurements_ladder_id", table_name="milestone_measurements")
    op.drop_table("milestone_measurements")
    op.drop_index("ix_milestones_ladder_id", table_name="milestones")
    op.drop_table("milestones")
    op.drop_table("milestone_ladders")
