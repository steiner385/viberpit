"""THE needs-you vs waiting definition (design 2026-10-14 §1): the pure mapping
from a queue skip reason to a waiting bucket, the owner logins, and the
assigned-to-owner rule."""
from __future__ import annotations

from dataclasses import replace

import pytest

from server.pm.attention import (WAITING_KEYS, assigned_to_owner, owner_logins,
                                 waiting_reason_for_skip)
from server.pm.queue import SKIP_REASONS, QueueRow


def _row(**over) -> QueueRow:
    base = QueueRow(
        ref="o/r#1", repo="o/r", number=1, product="p", title="t", body="", url="u",
        status="Ready", score=None, gh_created_at=None, gh_updated_at=None, assignees=(),
        author="tony", labels=(), open_children=0, open_blockers=(), live_session=False,
        claimed=False)
    return replace(base, **over)


def test_waiting_keys_are_the_stable_display_order():
    assert WAITING_KEYS == (
        "blocked", "boardBlocked", "boardHeld", "parked", "epicOpen", "inProgress",
        "tracker", "notNeeded", "assigned", "offQueue", "done", "bot", "untrusted",
        "environment")


@pytest.mark.parametrize(("reason", "row", "bucket"), [
    ("blocked", _row(open_blockers=("o/r#2",)), "blocked"),
    ("blocked", _row(status="Blocked"), "boardBlocked"),
    ("parked", _row(), "parked"),
    ("epic_open", _row(), "epicOpen"),
    ("tracker", _row(), "tracker"),
    ("not_needed", _row(), "notNeeded"),
    ("live_session", _row(), "inProgress"),
    ("claimed", _row(), "inProgress"),
    ("status", _row(status="In progress"), "inProgress"),
    ("status", _row(status="Held"), "boardHeld"),
    ("status", _row(status="Done"), "done"),
    ("status", _row(status="Backlog"), "offQueue"),
    ("status", _row(status=None), "offQueue"),
    ("assigned", _row(assignees=("someone",)), "assigned"),
    ("bot", _row(), "bot"),
    ("untrusted", _row(), "untrusted"),
    ("environment", _row(), "environment"),
])
def test_waiting_reason_for_skip(reason, row, bucket):
    assert waiting_reason_for_skip(reason, row) == bucket


def test_held_and_needs_spec_are_needs_you_not_waiting():
    assert waiting_reason_for_skip("held", _row(hold_kind="decision")) is None
    assert waiting_reason_for_skip("needs_spec", _row()) is None


def test_board_held_with_a_real_hold_is_not_waiting():
    assert waiting_reason_for_skip("status", _row(status="Held", hold_kind="owner_action")) is None


def test_assigned_to_the_owner_is_needs_you_not_waiting():
    row = _row(assignees=("Tony",))
    assert waiting_reason_for_skip("assigned", row, owners=frozenset({"tony"})) is None
    assert waiting_reason_for_skip("assigned", row, owners=frozenset({"alice"})) == "assigned"
    # Whatever else skipped it: owner-assigned is needs-you, never also waiting.
    parked_mine = _row(assignees=("tony",), labels=("parked",))
    assert waiting_reason_for_skip("parked", parked_mine, owners=frozenset({"tony"})) is None


def test_every_skip_reason_is_mapped():
    """A new queue skip reason must be classified here — never silently dropped."""
    for reason in SKIP_REASONS:
        out = waiting_reason_for_skip(reason, _row())
        assert out is None or out in WAITING_KEYS
    with pytest.raises(ValueError):
        waiting_reason_for_skip("no-such-reason", _row())


def test_owner_logins_are_the_human_trusted_authors():
    # Bound (#3147) and not-yet-bound user entries alike: owner matching is display-only,
    # never a trust decision.
    policy = {"trusted_authors": ["Tony#1234", "app/renovate", "dependabot[bot]", " Alice "]}
    assert owner_logins(policy) == frozenset({"tony", "alice"})
    assert owner_logins(None) == frozenset()


def test_assigned_to_owner():
    owners = frozenset({"tony"})
    assert assigned_to_owner(["TONY"], owners) is True
    assert assigned_to_owner(("bob", " tony "), owners) is True
    assert assigned_to_owner(["bob"], owners) is False
    assert assigned_to_owner(None, owners) is False
    assert assigned_to_owner(["tony"], frozenset()) is False
