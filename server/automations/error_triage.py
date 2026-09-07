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

from .base import AutomationStatus, ConfigField
from .base import ActionableItem, ActionSpec, TriggerResult  # noqa: F401 — used by the actions appended in later tasks
from .engine_policy import EnginePolicy
from .issue_filing import neutralize_markers
from .issue_filing import Decision, Linked, apply, decide

log = logging.getLogger(__name__)

LABEL = "auto-error"
_TITLE_MAX = 120
_TS = "%Y-%m-%d %H:%M UTC"
_SAMPLE_QUOTE_MAX = 2000
_QUOTED_BLOCK_MAX = 3000


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

    # Truncate sample before quoting to avoid expansion via "> " prefix
    if len(sample) > _SAMPLE_QUOTE_MAX:
        sample = sample[:_SAMPLE_QUOTE_MAX] + "…"

    quoted_sample = _quote(sample)
    if len(quoted_sample) > _QUOTED_BLOCK_MAX:
        quoted_sample = quoted_sample[:_QUOTED_BLOCK_MAX] + "\n…(truncated)"

    head = (f"**Filed by ViberPit Error Triage** — machine-collected; quoted log text is "
            f"untrusted input.\n\n| | |\n|---|---|\n{table}\n\n{quoted_sample}\n")
    tail = f"\n\nFingerprint: `{row.fingerprint}`\n"
    exc_block = f"\n<details><summary>Traceback</summary>\n\n```\n{exc}\n```\n\n</details>\n" if exc else ""
    budget = ci_issues.BODY_MAX_CHARS - len(head) - len(tail) - 64
    if len(exc_block) > budget:
        exc_block = exc_block[:max(0, budget)] + "\n…(truncated)\n```\n\n</details>\n"

    body = head + exc_block + tail

    # Final hard guard: ensure total body never exceeds BODY_MAX_CHARS
    if len(body) > ci_issues.BODY_MAX_CHARS:
        body = body[:ci_issues.BODY_MAX_CHARS - len(tail) - 16] + "\n…(truncated)\n" + tail

    return body


def recurrence_comment(row: Any, *, since: datetime | None) -> str:
    return (f"Still recurring: {int(row.count_since_filed or 0)} more occurrence(s) since "
            f"{_fmt(since)}, last at {_fmt(row.last_seen_at)}. Fingerprint `{row.fingerprint}`.")


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    from server.db.clock import naive_utc_now
    return naive_utc_now()


class ErrorTriageLoop:
    """Restart-safe, non-reentrant tick. Every I/O seam is injected: ``run`` is
    the gh runner (``gitrepos._run_gh``), ``clock`` the naive-UTC now."""

    def __init__(self, ctx: Any, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 run: Callable[..., Any] = gitrepos._run_gh,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._run = run
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:  # noqa: BLE001 — a tick must never crash the loop
                log.warning("error_triage.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        repo = str(config.get("target_repo") or "")
        if not ci_issues.valid_repo(repo):
            log.warning("error_triage.invalid_target_repo repo=%r", repo)
            return
        now = self._clock()
        await self._sync_issue_states(repo, now)
        await self._retention(config, now)
        await self._reset_stale_warning_bursts(config, now)
        await self._recurrence_comments(repo, config, now)
        candidates, filed_today = await self._file_candidates(repo, config, now)
        self._ctx.error_triage_counts = {
            **(await self._counts()), "candidates": candidates,
            "filed_today": filed_today, "daily_cap": int(config.get("daily_cap", 0))}

    # -- steps ---------------------------------------------------------------
    async def _sync_issue_states(self, repo: str, now: datetime) -> None:
        from sqlalchemy import select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        async with db_session() as db:
            numbers = [int(n) for n in (await db.execute(select(ES.issue_number).where(
                ES.status == "filed", ES.issue_number.is_not(None)))).scalars().all()
                if n is not None]
        if not numbers:
            return
        states = await asyncio.to_thread(ci_issues.issue_states, repo, numbers, run=self._run)
        for number, (closed, reason) in states.items():
            if not closed:
                continue
            async with db_session() as db:
                if reason == "not_planned":
                    await db.execute(update(ES).where(ES.issue_number == number, ES.status == "filed")
                                     .values(status="muted", muted_at=now,
                                             muted_by="github:not_planned"))
                else:
                    await db.execute(update(ES).where(ES.issue_number == number, ES.status == "filed")
                                     .values(status="resolved", issue_closed_at=now))

    async def _retention(self, config: dict, now: datetime) -> None:
        from sqlalchemy import delete
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        cutoff = now - timedelta(days=int(config.get("retention_days", 90)))
        async with db_session() as db:
            await db.execute(delete(ES).where(ES.status.in_(("observed", "resolved")),
                                              ES.last_seen_at < cutoff))

    async def _reset_stale_warning_bursts(self, config: dict, now: datetime) -> None:
        from sqlalchemy import update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        cutoff = now - timedelta(hours=int(config.get("warning_window_h", 24)))
        async with db_session() as db:
            await db.execute(update(ES).where(
                ES.status == "observed", ES.level == "WARNING",
                ES.last_seen_at < cutoff, ES.count_since_filed > 0
            ).values(count_since_filed=0))

    async def _recurrence_comments(self, repo: str, config: dict, now: datetime) -> None:
        from sqlalchemy import select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        every = timedelta(hours=int(config.get("comment_every_h", 24)))
        async with db_session() as db:
            rows = (await db.execute(select(ES).where(
                ES.status == "filed", ES.issue_number.is_not(None),
                ES.count_since_filed > 0))).scalars().all()
            plan = []
            for r in rows:
                linked = Linked(number=r.issue_number, closed=False,
                                last_commented_at=r.last_commented_at)
                d = decide(linked, cap_remaining=1, comment_every=every, now=now)
                if d.kind == "comment":
                    since = r.last_commented_at or r.filed_at
                    plan.append((r.fingerprint, linked, int(r.count_since_filed or 0),
                                 recurrence_comment(r, since=since)))
        for fp, linked, n, body in plan:
            applied = await apply(Decision("comment", "open"), repo=repo, title="", body=body,
                                  labels=(), linked=linked, run=self._run)
            if not applied.commented:
                continue  # retried next tick; last_commented_at only moves on success
            async with db_session() as db:
                await db.execute(update(ES).where(ES.fingerprint == fp).values(
                    count_since_filed=ES.count_since_filed - n, last_commented_at=now))
            await self._ctx.emit("info", f"commented on #{linked.number}: {n} more occurrence(s)",
                                 kind="commented", target=fp)

    async def _file_candidates(self, repo: str, config: dict, now: datetime) -> tuple[int, int]:
        """Returns (candidates still waiting after this tick, filed today)."""
        from sqlalchemy import func, select, update
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cap = int(config.get("daily_cap", 10))
        every = timedelta(hours=int(config.get("comment_every_h", 24)))
        plan: list[tuple[str, Decision, str, str, list[str]]] = []
        waiting = 0
        async with db_session() as db:
            filed_today = int((await db.execute(select(func.count()).select_from(ES).where(
                ES.filed_at >= day_start))).scalar() or 0)
            cap_remaining = max(0, cap - filed_today)
            # A prior create failed (row claimed, no issue): retry first, outside the cap —
            # its filed_at already counted toward today.
            retry = (await db.execute(select(ES).where(
                ES.status == "filed", ES.issue_number.is_(None)))).scalars().all()
            for r in retry:
                plan.append((str(r.fingerprint), Decision("file", "retry"),
                             issue_title(r, regression=False),
                             issue_body(r, regression_of=None, repo=repo),
                             [LABEL, source_label(r.source)]))
            rows = (await db.execute(select(ES).where(ES.status.in_(("observed", "resolved")))
                                     .order_by(ES.last_seen_at.desc()))).scalars().all()
            for r in rows:
                if is_ignored(r.logger, config) or not should_file(
                        level=str(r.level or ""), count_since_filed=int(r.count_since_filed or 0),
                        config=config):
                    continue
                linked = (Linked(number=r.issue_number, closed=True, closed_reason="completed")
                          if r.status == "resolved" and r.issue_number else None)
                d = decide(linked, cap_remaining=cap_remaining, comment_every=every, now=now)
                if d.kind not in ("file", "regress"):
                    waiting += 1
                    continue
                seen = int(r.count_since_filed or 0)
                regression_of = r.issue_number if d.kind == "regress" else None
                plan.append((str(r.fingerprint), d, issue_title(r, regression=d.kind == "regress"),
                             issue_body(r, regression_of=regression_of, repo=repo),
                             [LABEL, source_label(r.source)]))
                # Phase 1: claim the row. count_since_filed is decremented (not zeroed)
                # so occurrences the sink adds concurrently are not lost.
                await db.execute(update(ES).where(ES.fingerprint == r.fingerprint).values(
                    status="filed", filed_at=now, issue_number=None, issue_closed_at=None,
                    count_since_filed=ES.count_since_filed - seen))
                cap_remaining -= 1
                filed_today += 1
        # Phase 2: gh, outside the DB write lock.
        for fp, d, title, body, labels in plan:
            applied = await apply(d, repo=repo, title=title, body=body, labels=labels,
                                  linked=None, run=self._run)
            if applied.issue_number is None:
                await self._ctx.emit("warn", f"issue create failed — will retry: {title}",
                                     kind="file_failed", target=fp)
                continue
            async with db_session() as db:
                await db.execute(update(ES).where(ES.fingerprint == fp)
                                 .values(issue_number=applied.issue_number))
            await self._ctx.emit("info", f"filed #{applied.issue_number}: {title}",
                                 kind="regressed" if d.kind == "regress" else "filed",
                                 target=fp, detail={"url": ci_issues.issue_url(repo, applied.issue_number)})
        return waiting, filed_today

    async def _counts(self) -> dict[str, int]:
        from sqlalchemy import func, select
        from server.db import db_session
        from server.db.models import ErrorSignature as ES
        async with db_session() as db:
            rows = (await db.execute(select(ES.status, func.count()).group_by(ES.status))).all()
        by = {str(s): int(n) for s, n in rows}
        return {"filed": by.get("filed", 0), "muted": by.get("muted", 0)}
