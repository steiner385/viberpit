"""Assistant watch conditions → real `notify_event` event classes (spec v2 §4.7, Task 8.3).

Pure and DB-free by design (mirrors `server.sessions.notify_rules`): this module is the
single source of truth for which `watch(kind, target, condition)` calls a real, currently
-emitted event class, so `server/assistant/api.py` and its handlers never hardcode one.

`kind="session"` and `kind="account"` (#1354) were unwired at first: neither had anything
real to attach to (`NotificationRule` carried no per-session scope column, and no
`notify_event(...)` call site fired for an account going limited). Both gaps are closed:
  - `NotificationRule.scope_session_id` (+ `notify_rules.Event.session_id`) scopes a
    `session_ended` rule to one session id, the same way `scope_repo` scopes `pr_broke`.
    `SessionOrchestrator._maybe_push` fires it once per turn that hands control back
    (`status in {"done", "complete"}`) — see `server/sessions/orchestrator.py`.
  - `SessionOrchestrator._rotation_monitor` fires `account_limited` (debounced per
    account, cleared on recovery) the same place it already detects a usage-ceiling
    or unreachable-proxy rotation trigger.
Wiring either without a real, scoped event to attach to would create a watch that looks
like it works and silently never fires. `test_watch_conditions_are_actually_emitted`
(server/tests/test_assistant_watches_api.py) source-walks the real
`notify_event(event_class=...)` call sites and fails if this map ever drifts ahead of what
the code actually emits — extend both together, same pattern as
`server/tests/test_assistant_loop_guard.py`.
"""
from __future__ import annotations

# kind -> condition -> (event_class, NotificationRule scope field to set from `target`).
CONDITION_MAP: dict[str, dict[str, tuple[str, str]]] = {
    "pr": {
        # target is "owner/repo"; fires the next time that repo's CI breaks.
        "breaks": ("pr_broke", "scope_repo"),
    },
    "automation": {
        # target is an automation id (e.g. "backlog-runner"); fires on its next
        # error/warn-level activity.
        "error": ("automation_error", "scope_automation"),
        "warn": ("automation_warn", "scope_automation"),
    },
    "session": {
        # target is a session id; fires the next time that session hands a turn
        # back to the user (server/assistant/api.py's create_watch stores it verbatim
        # into scope_session_id — no transform needed, unlike the account wildcard
        # below).
        "finishes": ("session_ended", "scope_session_id"),
    },
    "account": {
        # target is an account name, or "any"/"*"/"all" for every account (the assistant
        # starter "Notify me if any account gets limited" has no specific account to
        # name) — create_watch maps that sentinel to a NULL scope_account, the existing
        # "wildcard" meaning scope_matches already gives a None scope field.
        "limited": ("account_limited", "scope_account"),
    },
}

VALID_KINDS: frozenset[str] = frozenset(CONDITION_MAP)


class UnknownWatchCondition(ValueError):
    """Raised for a kind/condition pair not in CONDITION_MAP."""


def resolve(kind: str, condition: str) -> tuple[str, str]:
    """Return (event_class, scope_field) for a kind/condition pair.

    Raises UnknownWatchCondition if the pair is not backed by a real, emitted event
    class — the caller (server/assistant/api.py) turns that into a 422.
    """
    by_condition = CONDITION_MAP.get(kind)
    if by_condition is None or condition not in by_condition:
        raise UnknownWatchCondition(
            f"unsupported watch kind/condition: {kind!r}/{condition!r}")
    return by_condition[condition]
