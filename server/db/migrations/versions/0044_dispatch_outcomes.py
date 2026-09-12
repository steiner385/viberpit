"""dispatch_outcomes: one durable row per Backlog Runner dispatch

Revision ID: 0044_dispatch_outcomes
Revises: 0043_triage_auto_undone

Reviewable record (spec 2026-09-12-product-automation-policy-design §6, phase 3).
Nothing runs `alembic upgrade` at deploy time: init_db's create_all builds this new
table on both dialects. Every column but id/created_at is nullable or defaulted, so
no hand-written default is needed and no RECONCILE_EXEMPT entry applies.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044_dispatch_outcomes"
down_revision = "0043_triage_auto_undone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dispatch_outcomes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("automation_id", sa.String(), nullable=True),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("target_ref", sa.String(), nullable=True),
        sa.Column("body_fingerprint", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("cause", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("first_pass_merge", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dispatch_outcomes_automation_id", "dispatch_outcomes",
                    ["automation_id"])
    op.create_index("ix_dispatch_outcomes_project_id", "dispatch_outcomes", ["project_id"])
    op.create_index("ix_dispatch_outcomes_target_ref", "dispatch_outcomes", ["target_ref"])
    op.create_index("ix_dispatch_outcomes_outcome", "dispatch_outcomes", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_dispatch_outcomes_outcome", table_name="dispatch_outcomes")
    op.drop_index("ix_dispatch_outcomes_target_ref", table_name="dispatch_outcomes")
    op.drop_index("ix_dispatch_outcomes_project_id", table_name="dispatch_outcomes")
    op.drop_index("ix_dispatch_outcomes_automation_id", table_name="dispatch_outcomes")
    op.drop_table("dispatch_outcomes")
