"""Session snooze: the pure rules (docs/superpowers/specs/2026-11-06-session-snooze-design.md).

A snooze is an overlay on a session, never a status: ``SessionRecord.snooze`` /
``sessions.snooze`` hold ``{"at", "until", "reason", "by"}`` (ISO strings, aware UTC)
or None. This module is THE place that validates one, decides when it is due, and
words the prompt a self-snoozed session wakes to. The orchestrator owns the writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

#: Who put the session to sleep. A ``session`` snooze is woken with a prompt (the agent
#: asked to be called back); a ``user`` snooze just comes back into the list.
SNOOZE_BY = ("user", "session")
#: A session's own snooze must name a time in this window: never a sleep nobody ends,
#: never a busy-poll. The 30 days match ``hand_back(recheck_at)``.
SELF_SNOOZE_MIN = timedelta(minutes=5)
SELF_SNOOZE_MAX = timedelta(days=30)
#: A person may snooze further out (or indefinitely: ``until=None``).
USER_SNOOZE_MAX = timedelta(days=365)
REASON_MAX = 500
#: Statuses a snooze makes no sense on: nothing is running yet (queued / held at the
#: model gate), or the session is over. Everything else — busy, idle, handed back,
#: complete — may be snoozed.
UNSNOOZABLE_STATUSES = frozenset({"queued", "pending_approval", "archived", "ended"})


class SnoozeError(ValueError):
    """A snooze request that cannot be honoured; the message is shown to the caller."""


def _parse_time(raw: str) -> datetime:
    try:
        at = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        raise SnoozeError("until must be an ISO-8601 timestamp, e.g. 2026-11-07T14:00:00Z") from None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_snooze(*, until: str | None, reason: str | None, by: str,
                 now: datetime | None = None) -> dict[str, Any]:
    """Validate a snooze request into the stored shape, or raise ``SnoozeError``."""
    if by not in SNOOZE_BY:
        raise SnoozeError(f"by must be one of {', '.join(SNOOZE_BY)}")
    now = _aware(now or datetime.now(timezone.utc))
    text = " ".join(str(reason or "").split()) or None
    if text is not None and len(text) > REASON_MAX:
        raise SnoozeError(f"reason must be at most {REASON_MAX} characters")
    if by == "session" and text is None:
        raise SnoozeError("reason is required: say what you are waiting for and what to "
                          "check when you wake")
    if until is None or until == "":
        if by == "session":
            raise SnoozeError("until is required: a session must name when to wake "
                              f"(at most {SELF_SNOOZE_MAX.days} days out)")
        at = None
    else:
        at = _parse_time(until)
        lo, hi = ((SELF_SNOOZE_MIN, SELF_SNOOZE_MAX) if by == "session"
                  else (timedelta(0), USER_SNOOZE_MAX))
        if at <= now + lo or at > now + hi:
            floor = "in the future" if by == "user" else f"at least {int(lo.total_seconds() // 60)} minutes out"
            raise SnoozeError(f"until must be {floor} and within {hi.days} days")
    return {"at": now.isoformat(), "until": at.astimezone(timezone.utc).isoformat() if at else None,
            "reason": text, "by": by}


def snooze_until(snooze: dict[str, Any] | None) -> datetime | None:
    """The aware-UTC wake time of *snooze*, or None (not snoozed, or indefinite)."""
    raw = (snooze or {}).get("until")
    if not raw:
        return None
    try:
        return _parse_time(raw)
    except SnoozeError:
        return None


def is_due(snooze: dict[str, Any] | None, now: datetime) -> bool:
    """True when a timed snooze's wake time has come. Indefinite never is."""
    at = snooze_until(snooze)
    return at is not None and at <= _aware(now)


def wake_prompt(snooze: dict[str, Any]) -> str:
    """The prompt a self-snoozed session wakes to: its own reason, and what to do next."""
    reason = escape(str(snooze.get("reason") or "(no reason recorded)"))
    return (
        "<snooze-wake>\n"
        f"<snoozed-at>{escape(str(snooze.get('at') or ''))}</snoozed-at>\n"
        f"<reason>{reason}</reason>\n"
        "<next-step>Your snooze has ended. Check whether what you were waiting for has "
        "happened. If it has, carry on with the work. If it has not and waiting still "
        "makes sense, call snooze again with a new time and end your turn. If waiting "
        "no longer makes sense, hand_back and say why.</next-step>\n"
        "</snooze-wake>"
    )
