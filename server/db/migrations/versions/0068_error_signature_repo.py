"""``error_signatures.repo`` (#1963).

Revision ID: 0068_error_signature_repo
Revises: 0067_archive_review_finding

The repo an ``ErrorSignature``'s ``issue_number`` was filed into (error-triage's
``target_repo`` at file time), not the fingerprint's origin. Issue numbers are
only unique within a repo, so without this column a ``target_repo`` repoint lets
an old row and a new one collide on the same number and be merged into one
family (``server/pm/error_family.py``) that they do not share.

Nullable, so pre-existing rows (filed before this column existed) keep matching
by number alone in ``families_for_refs`` — only a row written after this change
carries a repo that can disagree with the current target and be excluded.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s
``_reconcile_columns`` ALTER-ADDs the nullable column on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_error_signature_repo"
down_revision = "0067_archive_review_finding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("error_signatures",
                  sa.Column("repo", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("error_signatures", "repo")
