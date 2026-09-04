"""The ONE definition of "this session is stalled on usage" (constitution VI).

A paused session collapses every cause into the single ``needs_attention`` status;
the persisted ``paused_reason`` token is what distinguishes "every account is at its
ceiling until 12:10" (auto-recoverable, worth a batch rotate) from a crash cap. Both
wire serializers (SessionRecord.to_dict and api/sessions._record_to_dict) emit the
derived flag, and rotate_to_best / the recovery sweep read it — nothing re-derives it.
"""
from __future__ import annotations

# _pause_session reasons that mean "no account had headroom", plus the pinned-at-
# ceiling overlay the rotation monitor sets on a `limited` session.
USAGE_STALL_REASONS: frozenset[str] = frozenset(
    {"all_accounts_busy", "rotation_cap", "pinned_account_limited"})


def is_usage_stalled(status: str, paused_reason: str | None) -> bool:
    if status == "limited":
        return True
    return status == "needs_attention" and paused_reason in USAGE_STALL_REASONS
