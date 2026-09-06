"""Assistant watch conditions → real `notify_event` event classes (spec v2 §4.7, Task 8.3).

Pure and DB-free by design (mirrors `server.sessions.notify_rules`): this module is the
single source of truth for which `watch(kind, target, condition)` calls a real, currently
-emitted event class, so `server/assistant/api.py` and its handlers never hardcode one.

Only `kind="pr"` and `kind="automation"` are wired. The v2 design sketched `session` and
`account` kinds too, but neither has anything to actually attach to today:
  - `NotificationRule`/`notify_rules.Event` carry `scope_repo`/`scope_account`/
    `scope_automation` only — there is no per-session scope column, so a "tell me when
    THIS session finishes" watch cannot be filtered to one session.
  - No `notify_event(...)` call site anywhere in `server/` fires for an account going
    limited — "account.limited" would be a rule that can never match.
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
}

VALID_KINDS: frozenset[str] = frozenset(CONDITION_MAP)


class UnknownWatchCondition(ValueError):
    """Raised for a kind/condition pair not in CONDITION_MAP — including the
    unimplemented `session` and `account` kinds (see module docstring)."""


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
