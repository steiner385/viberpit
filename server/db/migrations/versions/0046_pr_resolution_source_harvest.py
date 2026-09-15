"""sessions.pr_resolution_source gains the value "harvest" (one-shot chained sessions).

Revision ID: 0046_pr_resolution_source_harvest
Revises: 0045_automation_ownership
Create Date: 2026-09-15

NO DDL — `upgrade()`/`downgrade()` are deliberately `pass`, exactly as in
`0034_pr_resolution_source_tool`. `sessions.pr_resolution_source` is an unconstrained
nullable `String` (`server/db/models.py`), so widening its domain from
{marker, tool, classifier, queue-drain} to {marker, tool, classifier, queue-drain,
harvest} changes only the semantics. Existing rows are untouched.

Why the value exists. A chained one-shot session — pm-classify, sdlc-materialize — is
told to "Return ONLY a JSON array/object", so it emits its fenced block and ends the
turn without calling `mark_complete` or `mark_no_pr`. `derive_status` returns `done`,
which is tier `your-turn`, and a `maintenance` session at `your-turn` lifts out of
Background into Active as if it had stalled. Telling the prompt to signal instead is
worse, not better: the effective goal of these sessions is their name ("pm classify
<slug>"), which `completion_bar._RESEARCH_RE` does not match, so a repo-attached run
reads as CODE work and `mark_complete` earns "open a PR and arm auto-merge" while
`mark_no_pr` lands on source "tool" — inside `SELF_CERTIFIED_SOURCES` — and trips the
unverified-self-claim branch.

"harvest" is written by `SessionOrchestrator._certify_harvested_complete` only after the
session's output has been parsed into rows (TriageProposal / MaterializationProposal).
That is the harness observing the deliverable, not the session asserting it finished,
which is precisely the distinction `SELF_CERTIFIED_SOURCES` encodes — so the value is
deliberately NOT a member of that frozenset, and `met_bar` trusts it.

The consumer-side counterpart landed in the same commit:
`server/automations/completion_bar.py:HARVEST_RESOLUTION_SOURCE`.
"""

revision = "0046_pr_resolution_source_harvest"
down_revision = "0045_automation_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: value-domain change only on an existing unconstrained nullable String."""
    pass


def downgrade() -> None:
    """No-op: nothing was altered on the way up."""
    pass
