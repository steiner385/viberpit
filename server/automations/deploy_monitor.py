"""Deploy Monitor — GitHub deployment statuses → incidents, alerts and issues
(spec docs/superpowers/specs/2026-09-12-deploy-monitor-design.md).

Two halves, the Error Triage / Control-Band mold: the pure half (contract
surface, ``evaluate_env`` policy, issue text) is at the top; ``DeployMonitorLoop``
(the tick, every I/O seam injected) and the card action provider follow.
The signal is read by server/github/deployments.py; the file/comment/regress
rule is server/automations/issue_filing.py.
"""
from __future__ import annotations

import asyncio  # noqa: F401  (the loop half uses it)
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable  # noqa: F401

from ccswitch import brand, gitrepos  # noqa: F401
from server.github import ci_issues
from server.github.deploy_envs import classify_env
from server.github.deployments import Deployment, by_env, parse_iso  # noqa: F401

from .base import ActionableItem, ActionSpec, AutomationStatus, ConfigField, TriggerResult  # noqa: F401
from .engine_policy import EnginePolicy
from .issue_filing import Decision, Linked, apply, decide, neutralize_markers  # noqa: F401

log = logging.getLogger(__name__)

LABEL = "deploy-monitor"
_TITLE_MAX = 120
_TS = "%Y-%m-%d %H:%M UTC"

#: GitHub DeploymentStatusState buckets. ``""`` is "no status yet" — a deployment
#: GitHub created but nothing has reported on, which is the stalled-deploy shape.
FAILING_STATES = frozenset({"FAILURE", "ERROR"})
PENDING_STATES = frozenset({"PENDING", "QUEUED", "WAITING", "IN_PROGRESS", ""})
HEALTHY_STATES = frozenset({"SUCCESS", "INACTIVE"})
INCIDENT_KINDS = ("failing", "stalled")


class DeployMonitor:
    id = "deploy-monitor"
    name = "Deploy Monitor"
    work_class = "maintenance"
    description = ("Watches GitHub deployment statuses per environment. A failing or "
                   "stalled deploy opens an incident, alerts once and — in file mode — "
                   "keeps one issue open in the repo until the environment recovers.")
    #: ``watch`` detects, feeds and alerts but never writes to GitHub; ``file`` adds
    #: the issue lifecycle. Turning it on is therefore a reversible, observable step.
    modes: tuple[str, ...] = ("off", "watch", "file")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY  # never spawns a session
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit to these repos (empty = all managed repos)."),
        ConfigField(key="all_envs", label="Watch every environment", type="bool", default=False,
                    help="Also watch environments that are not qa / prod / admin "
                         "(preview and ad-hoc environments)."),
        ConfigField(key="grace_min", label="Failure grace (minutes)", type="int", default=5,
                    min=0, help="A failed status must stand this long before it counts — "
                                "absorbs a platform's automatic retry."),
        ConfigField(key="stall_min", label="Stall after (minutes)", type="int", default=30,
                    min=1, help="A deployment still in progress this long is an incident."),
        ConfigField(key="comment_every_h", label="Recurrence comment gap (hours)", type="int",
                    default=6, min=1),
        ConfigField(key="daily_cap", label="New issues per day", type="int", default=10, min=0),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "deploy_monitor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        repos, envs = int(counts.get("repos", 0)), int(counts.get("envs", 0))
        failing, stalled = int(counts.get("failing", 0)), int(counts.get("stalled", 0))
        incidents = int(counts.get("incidents", failing + stalled))
        text = (f"{repos} repo{'' if repos == 1 else 's'} · {envs} envs watched · "
                f"{incidents} incident{'' if incidents == 1 else 's'}")
        if failing:
            level = "error"
        elif stalled:
            level = "warn"
        elif envs:
            level = "active"
        else:
            level = "idle"
        return AutomationStatus(text=text, level=level, counts=dict(counts))

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        ...  # the loop reads mode/config each tick


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvVerdict:
    env: str
    kind: str        # healthy | failing | stalled | unknown
    newest: Deployment
    streak: int      # consecutive newest-first deployments with a failing status


def evaluate_env(env: str, deployments: list[Deployment], *, now: datetime,
                 grace_min: int, stall_min: int) -> EnvVerdict | None:
    """Classify one environment from its newest-first deployments (spec §5.2).

    ``unknown`` means "say nothing this tick" — a young failure inside the grace
    window, a young in-progress deploy, or a state we do not model. The loop
    neither opens nor closes an incident on it."""
    if not deployments:
        return None
    newest = deployments[0]
    state = newest.status_state
    if state in FAILING_STATES:
        at = newest.status_at or newest.created_at
        if at is not None and now - at < timedelta(minutes=int(grace_min)):
            return EnvVerdict(env, "unknown", newest, 0)
        streak = 0
        for d in deployments:
            if d.status_state not in FAILING_STATES:
                break
            streak += 1
        return EnvVerdict(env, "failing", newest, streak)
    if state in PENDING_STATES:
        at = newest.created_at
        if at is not None and now - at >= timedelta(minutes=int(stall_min)):
            return EnvVerdict(env, "stalled", newest, 1)
        return EnvVerdict(env, "unknown", newest, 0)
    if state in HEALTHY_STATES:
        return EnvVerdict(env, "healthy", newest, 0)
    return EnvVerdict(env, "unknown", newest, 0)


def watched_env(env: str, config: dict) -> bool:
    """Preview / ad-hoc environments never open incidents unless asked to."""
    return bool(config.get("all_envs")) or classify_env(env) is not None


def incident_key(repo: str, env: str) -> str:
    return f"{repo}#{env}"


def split_key(key: str) -> tuple[str, str]:
    """Inverse of ``incident_key``. A repo is ``owner/name`` and never holds ``#``,
    so the FIRST ``#`` is the seam even if the env name contains one."""
    repo, _, env = key.partition("#")
    return repo, env


def _fmt(value: object) -> str:
    dt = value if isinstance(value, datetime) else parse_iso(value)
    return dt.strftime(_TS) if dt else "?"


def _short(sha: object) -> str:
    return str(sha or "")[:10] or "?"


def issue_title(repo: str, env: str, state: dict) -> str:
    word = "stalled" if state.get("kind") == "stalled" else "failing"
    return f"[{LABEL}] {env} deploy {word} — {repo} @ {_short(state.get('sha'))}"[:_TITLE_MAX]


def _cell(value: object) -> str:
    text = neutralize_markers(str(value)) if value not in (None, "") else "—"
    return text.replace("|", "\\|").replace("\n", " ")


def issue_body(repo: str, env: str, state: dict, *, regression_of: int | None) -> str:
    """Machine-collected; GitHub-sourced text (ref, creator, URLs) is untrusted
    and passed through ``neutralize_markers``. Hard-capped at BODY_MAX_CHARS."""
    rows = [
        ("Repository", repo),
        ("Environment", env),
        ("Kind", state.get("kind")),
        ("Newest deployment", f"`{_short(state.get('sha'))}` on {_cell(state.get('ref'))}"),
        ("First failing deployment", f"`{_short(state.get('first_sha'))}`"),
        ("Consecutive failures", int(state.get("streak") or 0)),
        ("First seen", _fmt(state.get("first_seen_at"))),
        ("Last seen", _fmt(state.get("last_seen_at"))),
        ("Created by", state.get("creator")),
        ("Log", state.get("log_url")),
        ("Environment URL", state.get("env_url")),
    ]
    if regression_of is not None:
        rows.append(("Regression of", f"#{int(regression_of)}"))
    table = "\n".join(f"| {k} | {_cell(v)} |" for k, v in rows)
    head = (f"**Filed by {brand.NAME} Deploy Monitor** from GitHub deployment statuses — "
            f"the environment's newest deployment did not reach SUCCESS.\n\n"
            f"| | |\n|---|---|\n{table}\n")
    tail = f"\nTarget: `{incident_key(repo, env)}`\n"
    body = head + tail
    if len(body) > ci_issues.BODY_MAX_CHARS:
        body = body[:ci_issues.BODY_MAX_CHARS - len(tail) - 16] + "\n…(truncated)\n" + tail
    return body


def recurrence_comment(state: dict) -> str:
    return (f"Still {state.get('kind', 'failing')}: newest deployment `{_short(state.get('sha'))}` "
            f"({int(state.get('streak') or 0)} consecutive), last seen {_fmt(state.get('last_seen_at'))}.")


def recovery_comment(env: str, sha: str, now: datetime) -> str:
    return f"Recovered: `{env}` deployed `{_short(sha)}` successfully at {now.strftime(_TS)}. Closing."


async def notify_deploy_failed(title: str, body: str, *, repo: str, url: str | None) -> None:
    """The one ``deploy_failed`` call site. Fans out like ``pr_broke`` when no
    rules are configured: a failing deploy is what a user with no rules still
    expects to hear about. (test_assistant_watches_api source-walks this literal.)"""
    from server.sessions.push import notify_event
    await notify_event("deploy_failed", title, body, repo=repo, url=url,
                       fanout_when_unconfigured=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
