"""Worktree controls for a session (D-19): preflight, Discard and Repair.

The orchestrator owns the work (``SessionOrchestrator.worktree_preflight`` /
``discard_worktree`` / ``repair_worktree``, run on the daemon that owns the session);
this module is the shared vocabulary every hop agrees on: the refusal codes, the
error that carries one across the daemon's RPC envelope, and the copy and HTTP
status the edge answers with (``session_actions``).

* **Discard** ends the session and removes its managed worktree. Idle only. Before
  anything is touched, every dirty tree and every commit the removal would leave
  unreferenced is kept as a recovery ref (``worktrees.snapshot_before_discard``); a
  failure to keep one refuses the discard. Only a session branch
  (``brand.is_session_branch``) is deleted — ``remove_worktree`` enforces it.
* **Repair** recreates a missing or empty managed worktree from the session's branch
  through the same gate a driver build uses (``_ensure_session_cwd``). A present
  tree is never touched.
"""
from __future__ import annotations

WORKTREE_CONFIRM_REQUIRED = "WORKTREE_CONFIRM_REQUIRED"
WORKTREE_SESSION_BUSY = "WORKTREE_SESSION_BUSY"
WORKTREE_NOT_MANAGED = "WORKTREE_NOT_MANAGED"
WORKTREE_MISSING = "WORKTREE_MISSING"
WORKTREE_SHARED = "WORKTREE_SHARED"
WORKTREE_SNAPSHOT_FAILED = "WORKTREE_SNAPSHOT_FAILED"
WORKTREE_DISCARD_FAILED = "WORKTREE_DISCARD_FAILED"
WORKTREE_REMOVE_FAILED = "WORKTREE_REMOVE_FAILED"
WORKTREE_PRESENT = "WORKTREE_PRESENT"
WORKTREE_NOT_EMPTY = "WORKTREE_NOT_EMPTY"
WORKTREE_REPAIR_FAILED = "WORKTREE_REPAIR_FAILED"

#: Every code a daemon may raise; a remote edge rebuilds only these.
WORKTREE_CONTROL_CODES: frozenset[str] = frozenset({
    WORKTREE_SESSION_BUSY, WORKTREE_NOT_MANAGED, WORKTREE_MISSING, WORKTREE_SHARED,
    WORKTREE_SNAPSHOT_FAILED, WORKTREE_DISCARD_FAILED, WORKTREE_REMOVE_FAILED,
    WORKTREE_PRESENT, WORKTREE_NOT_EMPTY, WORKTREE_REPAIR_FAILED,
})

#: Failures after the recovery refs were written: the refs are kept and returned.
_SERVER_FAILURES = frozenset({WORKTREE_SNAPSHOT_FAILED, WORKTREE_DISCARD_FAILED,
                              WORKTREE_REMOVE_FAILED})

MESSAGES: dict[str, str] = {
    WORKTREE_CONFIRM_REQUIRED: "Confirm the discard: it ends the session and removes its "
                               "worktree.",
    WORKTREE_SESSION_BUSY: "The session is busy. Wait for the turn to finish (or Interrupt "
                           "it), answer any open card, and let background tasks end first.",
    WORKTREE_NOT_MANAGED: "This session does not run in a worktree ViberPit created, so it "
                          "has nothing to discard or repair.",
    WORKTREE_MISSING: "The worktree is already gone from this host. Repair it, or archive "
                      "the session.",
    WORKTREE_SHARED: "Another session runs in this worktree, so it cannot be removed.",
    WORKTREE_SNAPSHOT_FAILED: "The worktree's changes could not be saved to a recovery "
                              "ref, so nothing was discarded.",
    WORKTREE_DISCARD_FAILED: "The session could not be ended. Its changes are saved in "
                             "the recovery refs listed.",
    WORKTREE_REMOVE_FAILED: "The session was archived, but its worktree could not be "
                            "removed. Its changes are saved in the recovery refs listed.",
    WORKTREE_PRESENT: "The worktree is present; there is nothing to repair.",
    WORKTREE_NOT_EMPTY: "The worktree's path holds files that are not a git worktree; "
                        "repair never touches them.",
    WORKTREE_REPAIR_FAILED: "The worktree could not be recreated: its branch is not in this "
                            "host's clone, or the session belongs to another host.",
}


def http_status(code: str) -> int:
    """The edge's answer for *code*: 400 a missing confirm, 500 a failure after the
    refs were written (or the refs themselves), else 409 (a state that refuses it)."""
    if code == WORKTREE_CONFIRM_REQUIRED:
        return 400
    return 500 if code in _SERVER_FAILURES else 409


class WorktreeControlError(Exception):
    """A preflight-checked refusal or a failure of Discard / Repair. ``error_data``
    rides the daemon's RPC envelope (code plus ``extra``: the recovery refs and restore
    command once they exist), so a remote edge rebuilds it, and marks it an expected
    refusal the daemon logs at INFO."""

    def __init__(self, code: str, extra: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.extra: dict = dict(extra or {})
        self.error_data = {"code": code, **self.extra}
