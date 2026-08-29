"""add ix_ci_test_result_repo_created_ts — composite index for the CI/CD doctor tick

Revision ID: 0008_ci_test_result_repo_created_ts_index
Revises: 0007_app_settings_auto_archive_enabled
Create Date: 2026-08-29

Every CI/CD-doctor tick asks ci_test_result for "this repo, this window"
(``cicd_doctor.flake_counts_stmt``). The table carries single-column indexes on
``repo`` and on ``created_ts``, and Postgres would use neither for that pair: on
2026-08-29 it chose a Parallel Seq Scan over all 26,427,569 rows, 65s per tick
per repo.

    Parallel Seq Scan on ci_test_result  (cost=0.00..1785068.85 rows=5747163)
      Filter: repo = 'cairnea/KinDash' AND outcome = ANY (...) AND created_ts >= ...

IMPORTANT — this one does NOT self-apply. init_db()'s reconcile ALTER-ADDs missing
*columns*; it does not create indexes, and ``create_all`` never touches an existing
table. Nothing runs ``alembic upgrade`` at deploy time either. On a live database
this must be created by hand, CONCURRENTLY so it takes no lock on a 26M-row table
that the daemon is actively writing:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ci_test_result_repo_created_ts
        ON ci_test_result (repo, created_ts);

(CONCURRENTLY cannot run inside a transaction, so it cannot be expressed in the
op.create_index below — that form is for fresh databases created from the models.)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008_ci_test_result_repo_created_ts_index"
down_revision: Union[str, None] = "0007_app_settings_auto_archive_enabled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_ci_test_result_repo_created_ts", "ci_test_result",
                    ["repo", "created_ts"])


def downgrade() -> None:
    op.drop_index("ix_ci_test_result_repo_created_ts", table_name="ci_test_result")
