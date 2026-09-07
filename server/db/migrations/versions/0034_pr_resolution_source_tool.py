"""sessions.pr_resolution_source gains the value "tool" (marker → tool migration §8).

Revision ID: 0034_pr_resolution_source_tool
Revises: 0033_triage_items
Create Date: 2026-09-07

NO DDL — `upgrade()`/`downgrade()` are deliberately `pass`. `sessions.pr_resolution_source`
is already an unconstrained nullable `String` (`server/db/models.py`), so widening its
domain from {marker, classifier, queue-drain} to {marker, tool, classifier, queue-drain}
changes only the semantics, not the schema. Existing rows are untouched: "marker" keeps
its meaning, and "tool" is written only by sessions on an engine that carries the
`mark_no_pr` signalling tool.

This revision exists because the repo requires a reviewable record for every schema
change, semantics included — nothing runs `alembic upgrade` at deploy time. The
consumer-side counterpart landed in the same commit:
`server/automations/completion_bar.py:SELF_CERTIFIED_SOURCES` is a frozenset containing
both "marker" and "tool", so a tool-sourced no-PR claim is still treated as the session's
own unverified claim rather than a verdict.
"""

revision = "0034_pr_resolution_source_tool"
down_revision = "0033_triage_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: value-domain change only on an existing unconstrained nullable String."""
    pass


def downgrade() -> None:
    """No-op: nothing was altered on the way up."""
    pass
