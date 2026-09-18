"""sessions.pr_resolution_source gains the value "observed" (map-diff sessions).

Revision ID: 0051_pr_resolution_source_observed
Revises: 0050_dispatch_kind
Create Date: 2026-09-18

NO DDL — `upgrade()`/`downgrade()` are deliberately `pass`, exactly as in
`0046_pr_resolution_source_harvest`. `sessions.pr_resolution_source` is an unconstrained
nullable `String` (`server/db/models.py`), so widening its domain from
{marker, tool, classifier, queue-drain, harvest} to {…, observed} changes only the
semantics. Existing rows are untouched.

Why the value exists. A pm-onboard map-diff session is asked to add ONE feature doc for
a no-fit cluster. When the cluster turns out to be covered already — by a doc under a
different slug — the right answer is `mark_no_pr` (not-needed). That lands on source
"tool", inside `completion_bar.SELF_CERTIFIED_SOURCES`, on a repo-attached goal that
does not read as research, so the completion guard nudges "open a PR and arm
auto-merge" (up to `COMPLETE_NUDGE_CAP` times) unless the Haiku salvage classifier
happens to confirm. The agent hands back, `derive_status` returns `waiting_question`,
and a `maintenance` session lifts out of Background into Active on a needs-you pill.

"observed" is written by `SessionOrchestrator._pm_onboard_observe_map_diff_not_needed`
only after the harness has read the feature docs on `origin/<default>` and found the
suggested doc already present, or at least half of the cluster's driving item numbers
already cited there. That is the harness observing the repository, not the session
asserting it finished — the same distinction `harvest` (0046) encodes for classify
sessions — so the value is deliberately NOT a member of `SELF_CERTIFIED_SOURCES`, and
`met_bar` trusts it.

The consumer-side counterpart landed in the same commit:
`server/automations/completion_bar.py:OBSERVED_RESOLUTION_SOURCE`.
"""

revision = "0051_pr_resolution_source_observed"
down_revision = "0050_dispatch_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
