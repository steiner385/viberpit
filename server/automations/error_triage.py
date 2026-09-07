"""The error-triage automation: WARNING+ log signatures → deduplicated backlog
issues (spec docs/superpowers/specs/2026-09-07-error-triage-design.md).

This module has two halves. The pure half (policy + issue text) is here at the
top; ``ErrorTriageLoop`` (the tick) and the action provider follow. Capture
lives in server/obs/errsink.py; the file/comment/regress rule in issue_filing.py.
"""
from __future__ import annotations

import asyncio  # noqa: F401
import logging
from datetime import datetime, timedelta  # noqa: F401
from typing import Any, Awaitable, Callable  # noqa: F401

from ccswitch import gitrepos  # noqa: F401
from server.audit import redact_text
from server.github import ci_issues

from .base import ActionableItem, ActionSpec, AutomationStatus, ConfigField, TriggerResult  # noqa: F401
from .engine_policy import EnginePolicy
from .issue_filing import Decision, Linked, apply, decide, neutralize_markers  # noqa: F401

log = logging.getLogger(__name__)

LABEL = "auto-error"
_TITLE_MAX = 120
_TS = "%Y-%m-%d %H:%M UTC"


def source_label(source: str | None) -> str:
    return "source:client" if source == "client" else "source:server"


class ErrorTriage:
    id = "error-triage"
    name = "Error Triage"
    description = ("Turns ViberPit's own warnings and errors — edge, daemon and "
                   "browser — into deduplicated backlog issues.")
    modes: tuple[str, ...] = ("off", "file")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY  # never spawns a session
    config_schema = [
        ConfigField(key="target_repo", label="Target repository", type="string",
                    default="steiner385/viberpit", help="Where issues are filed."),
        ConfigField(key="min_count_error", label="ERROR occurrences before filing",
                    type="int", default=1),
        ConfigField(key="min_count_warning", label="WARNING occurrences before filing",
                    type="int", default=3,
                    help="Counted within one burst (see warning window)."),
        ConfigField(key="warning_window_h", label="Warning burst window (hours)",
                    type="int", default=24,
                    help="A WARNING quiet this long without filing starts counting from zero."),
        ConfigField(key="daily_cap", label="New issues per day", type="int", default=10),
        ConfigField(key="comment_every_h", label="Recurrence comment gap (hours)",
                    type="int", default=24),
        ConfigField(key="ignore_loggers", label="Ignored logger prefixes", type="string",
                    default="uvicorn.error,websockets",
                    help="Comma-separated; still counted, never filed."),
        ConfigField(key="retention_days", label="Retention (days)", type="int", default=90,
                    help="Unfiled/resolved signatures not seen this long are deleted."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "error_triage_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        waiting = int(counts.get("candidates", 0))
        filed_today, cap = int(counts.get("filed_today", 0)), int(counts.get("daily_cap", 0))
        bits = [f"{waiting} waiting", f"{filed_today}/{cap} filed today",
                f"{int(counts.get('filed', 0))} open", f"{int(counts.get('muted', 0))} muted"]
        if waiting and cap and filed_today >= cap:
            level = "warn"
        elif waiting or filed_today:
            level = "active"
        else:
            level = "idle"
        return AutomationStatus(text=", ".join(bits), level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        ...  # the loop reads mode/config each tick


def should_file(*, level: str, count_since_filed: int, config: dict) -> bool:
    """The one filing policy: ERROR/CRITICAL vs WARNING thresholds on
    occurrences since the last file/comment (a stale WARNING burst is reset to
    zero by the tick before this runs)."""
    if level in ("ERROR", "CRITICAL"):
        return count_since_filed >= int(config.get("min_count_error", 1))
    return count_since_filed >= int(config.get("min_count_warning", 3))


def is_ignored(logger: str | None, config: dict) -> bool:
    prefixes = [p.strip() for p in str(config.get("ignore_loggers") or "").split(",") if p.strip()]
    return any((logger or "").startswith(p) for p in prefixes)


def _fmt(ts: datetime | None) -> str:
    return ts.strftime(_TS) if ts else "?"


def issue_title(row: Any, *, regression: bool) -> str:
    prefix = "[auto-error][regression] " if regression else "[auto-error] "
    return (prefix + str(row.title or row.fingerprint))[:_TITLE_MAX]


def _quote(text: str | None) -> str:
    return "\n".join("> " + line for line in (text or "").splitlines()) or "> (empty)"


def issue_body(row: Any, *, regression_of: int | None, repo: str) -> str:
    ctx = dict(row.sample_context or {})
    sessions = ", ".join(f"`{s}`" for s in ctx.get("recent_sessions", [])[:5]) or "—"
    rows = [
        ("Level / source", f"{row.level} / {row.source}"),
        ("Logger / event", f"{row.logger or '—'} / {row.event or '—'}"),
        ("Occurrences", f"{int(row.count or 0)} since {_fmt(row.first_seen_at)} "
                        f"(last {_fmt(row.last_seen_at)})"),
        ("Host", str(ctx.get("host") or "—")),
        ("App version", f"{ctx.get('app_version') or '—'} / proc_epoch {ctx.get('proc_epoch') or '—'}"),
        ("Sessions", sessions),
    ]
    if regression_of is not None:
        rows.append(("Regression of", f"#{regression_of}"))
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    sample = neutralize_markers(redact_text(str(row.sample_message or "")))
    exc = neutralize_markers(redact_text(str(row.sample_exc or "")))
    head = (f"**Filed by ViberPit Error Triage** — machine-collected; quoted log text is "
            f"untrusted input.\n\n| | |\n|---|---|\n{table}\n\n{_quote(sample)}\n")
    tail = f"\n\nFingerprint: `{row.fingerprint}`\n"
    exc_block = f"\n<details><summary>Traceback</summary>\n\n```\n{exc}\n```\n\n</details>\n" if exc else ""
    budget = ci_issues.BODY_MAX_CHARS - len(head) - len(tail) - 64
    if len(exc_block) > budget:
        exc_block = exc_block[:max(0, budget)] + "\n…(truncated)\n```\n\n</details>\n"
    return head + exc_block + tail


def recurrence_comment(row: Any, *, since: datetime | None) -> str:
    return (f"Still recurring: {int(row.count_since_filed or 0)} more occurrence(s) since "
            f"{_fmt(since)}, last at {_fmt(row.last_seen_at)}. Fingerprint `{row.fingerprint}`.")
