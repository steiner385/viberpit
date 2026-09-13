"""Deploy Monitor — GitHub deployment statuses → incidents, alerts and issues
(spec docs/superpowers/specs/2026-09-12-deploy-monitor-design.md).

Two halves, the Error Triage / Control-Band mold: the pure half (contract
surface, ``evaluate_env`` policy, issue text) is at the top; ``DeployMonitorLoop``
(the tick, every I/O seam injected) and the card action provider follow.
The signal is read by server/github/deployments.py; the file/comment/regress
rule is server/automations/issue_filing.py.
"""
from __future__ import annotations

from .context import observation_failed, publish_observation

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ccswitch import brand, gitrepos
from server.github import ci_issues
from server.github.deploy_envs import classify_env
from server.github.deployments import Deployment, by_env, parse_iso

from .base import ActionableItem, ActionSpec, AutomationStatus, ConfigField, TriggerResult
from .engine_policy import EnginePolicy
from .issue_filing import Decision, Linked, apply, decide, neutralize_markers

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
    #: ``watch`` never opens an issue or comments on one; an issue the incident
    #: already holds (filed earlier in ``file`` mode, or via the card's *File issue*
    #: action) is still closed with a recovery comment on recovery, and issue state
    #: is still read. ``file`` adds opening issues and recurrence comments. Turning
    #: it on is therefore a reversible, observable step.
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
        muted = int(counts.get("muted", 0))
        incidents = int(counts.get("incidents", failing + stalled + muted))
        text = (f"{repos} repo{'' if repos == 1 else 's'} · {envs} envs watched · "
                f"{incidents} incident{'' if incidents == 1 else 's'}")
        if muted:
            text += f" ({muted} muted)"
        # The level is driven by the non-muted counts only: a muted incident is
        # deliberately silenced by an operator (or an issue closed not_planned) and
        # must not keep the card red/yellow while it waits for recovery.
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
        ("Newest deployment", f"`{_short(state.get('sha'))}` on {state.get('ref') or '—'}"),
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
    return (f"Recovered: `{neutralize_markers(env)}` deployed `{_short(sha)}` successfully "
            f"at {now.strftime(_TS)}. Closing.")


def dismiss_comment(env: str) -> str:
    return (f"Dismissed by operator; `{neutralize_markers(env)}` no longer reports "
            f"deployments.")


async def notify_deploy_failed(title: str, body: str, *, repo: str, url: str | None) -> None:
    """The one ``deploy_failed`` call site. Fans out like ``pr_broke`` when no
    rules are configured: a failing deploy is what a user with no rules still
    expects to hear about. (test_assistant_watches_api source-walks this literal.)"""
    from server.sessions.push import notify_event
    await notify_event("deploy_failed", title, body, repo=repo, url=url,
                       fanout_when_unconfigured=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListRepos = Callable[[dict], Awaitable[list[str]]]
FetchDeployments = Callable[[list[str]], Awaitable[dict[str, list[Deployment] | None]]]
Notify = Callable[..., Awaitable[None]]

_DAY = timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class DeployMonitorLoop:
    """Restart-safe, non-reentrant tick. Every I/O seam is injected: ``run`` is
    the gh runner (``gitrepos._run_gh``), ``notify`` the ``deploy_failed`` push,
    ``clock`` an aware-UTC now. Incident state lives in the ctx's target-state
    rows keyed ``repo#env`` (spec §5.3)."""

    def __init__(self, ctx: Any, *,
                 read_config: ReadConfig,
                 list_repos: ListRepos,
                 fetch_deployments: FetchDeployments,
                 run: Callable[..., Any] = gitrepos._run_gh,
                 notify: Notify = notify_deploy_failed,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch = fetch_deployments
        self._run = run
        self._notify = notify
        self._clock = clock
        self._lock = asyncio.Lock()
        self._unreadable: set[str] = set()        # repos whose last fetch failed

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:  # noqa: BLE001 — a tick must never crash the loop
                observation_failed(self._ctx)
                log.warning("deploy_monitor.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        now = self._clock()
        repos = []
        for repo in await self._list_repos(config):
            if ci_issues.valid_repo(str(repo)):
                repos.append(str(repo))
            else:
                log.warning("deploy_monitor.invalid_repo repo=%r", repo)
        fetched = await self._fetch(repos) if repos else {}
        states = await self._ctx.list_target_states()
        watched = 0
        repos_set = set(repos)
        seen_envs: set[str] = set()   # keys seen in a SUCCESSFUL read this tick, watched or not
        for repo in repos:
            deps = fetched.get(repo)
            if deps is None:
                if repo not in self._unreadable:
                    self._unreadable.add(repo)
                    await self._ctx.emit("warn", f"could not read deployments for {repo}; "
                                         "leaving its incidents untouched",
                                         kind="fetch_failed", target=repo)
                continue
            if repo in self._unreadable:
                self._unreadable.discard(repo)
                await self._ctx.emit("info", f"deployments readable again for {repo}",
                                     kind="fetch_recovered", target=repo)
            # A read, not a write: runs whenever any of this repo's rows holds an
            # open issue — whether the loop opened it in file mode, or an
            # operator's "File issue" card action opened it while in watch mode —
            # so a human closing it (e.g. not_planned) is learned regardless of
            # mode. Only OPENING issues from the loop and recurrence comments
            # stay file-mode-only (spec §5.4).
            has_open_issue = any(split_key(k)[0] == repo and s.get("issue_number")
                                 and not s.get("issue_closed") for k, s in states.items())
            if mode == "file" or has_open_issue:
                await self._sync_issue_states(repo, states, now)
            for env, env_deps in by_env(deps).items():
                seen_envs.add(incident_key(repo, env))
                if not watched_env(env, config):
                    continue
                watched += 1
                verdict = evaluate_env(env, env_deps, now=now,
                                       grace_min=int(config.get("grace_min", 5)),
                                       stall_min=int(config.get("stall_min", 30)))
                key = incident_key(repo, env)
                await self._reconcile(repo, env, key, states.get(key), verdict, mode, config, now, states)
        final_states = await self._ctx.list_target_states()
        await self._sweep_vanished(final_states, fetched, repos_set, seen_envs)
        self._stash_counts(len(repos), watched, final_states,
                           degraded=any(fetched.get(repo) is None for repo in repos))

    async def _sweep_vanished(self, states: dict[str, dict], fetched: dict[str, list[Deployment] | None],
                              repos_set: set[str], seen_envs: set[str]) -> None:
        """A row whose repo left the watched set, or whose env is missing from an
        otherwise-successful read (renamed service, rolled out of the 50-deployment
        window), is never auto-deleted — that would silently drop an ongoing
        incident on a rename. Emit ``vanished`` once (``vanished_noted`` guards it,
        cleared if the env reappears) and leave the row for an operator to dismiss."""
        for key, s in states.items():
            repo, env = split_key(key)
            if repo not in repos_set:
                vanished = True
            elif fetched.get(repo) is None:
                vanished = False   # this tick's read failed — leave untouched (spec §7)
            else:
                vanished = key not in seen_envs
            if vanished:
                if not s.get("vanished_noted"):
                    s["vanished_noted"] = True
                    await self._ctx.set_target_state(key, s)
                    await self._ctx.emit("info", f"{repo} · {env} no longer reports deployments",
                                         kind="vanished", target=key)
            elif s.get("vanished_noted"):
                s["vanished_noted"] = False
                await self._ctx.set_target_state(key, s)

    # -- issue state sync ------------------------------------------------------
    async def _sync_issue_states(self, repo: str, states: dict[str, dict], now: datetime) -> None:
        """Learn which of this repo's linked issues a human closed. A read, so
        the caller runs it in watch mode too whenever a row already holds an
        open issue (e.g. an operator's "File issue" card action). ``not_planned``
        mutes the incident until recovery; any other close is an acknowledgment
        of the CURRENT sha (a later sha regresses — spec §5.4)."""
        # Deliberately NOT filtering out muted rows: a manual ``mute`` action
        # (unlike a not_planned close) leaves ``issue_closed`` False, so without
        # this the loop would never learn a human closed that issue — and
        # ``_recover`` would later PATCH an issue that is already closed.
        keys = [k for k, s in states.items() if split_key(k)[0] == repo
                and s.get("issue_number") and not s.get("issue_closed")]
        if not keys:
            return
        numbers = [int(states[k]["issue_number"]) for k in keys]
        closed = await asyncio.to_thread(ci_issues.issue_states, repo, numbers, run=self._run)
        for key in keys:
            state = states[key]
            info = closed.get(int(state["issue_number"]))
            if not info or not info[0]:
                continue
            _, reason = info
            if reason == "not_planned":
                state["muted"] = True
                state["issue_closed"] = True
                await self._ctx.emit("info", f"#{state['issue_number']} closed as not planned — "
                                     f"{key} muted until it recovers", kind="muted", target=key)
            else:
                state["issue_closed"] = True
                state["closed_sha"] = state.get("sha")
            await self._ctx.set_target_state(key, state)

    # -- per-env reconcile -----------------------------------------------------
    async def _reconcile(self, repo: str, env: str, key: str, state: dict | None,
                         verdict: EnvVerdict | None, mode: str, config: dict, now: datetime,
                         states: dict[str, dict]) -> None:
        if verdict is None or verdict.kind == "unknown":
            return
        if verdict.kind == "healthy":
            if state:
                await self._recover(repo, env, key, state, verdict, now)
            return
        if not state:
            await self._open(repo, env, key, verdict, mode, config, now, states)
        else:
            await self._ongoing(repo, env, key, state, verdict, mode, config, now, states)

    def _measure(self, state: dict, verdict: EnvVerdict, now: datetime) -> dict:
        d = verdict.newest
        state.update(kind=verdict.kind, sha=d.sha, ref=d.ref, creator=d.creator,
                     streak=max(int(verdict.streak), 1), last_seen_at=_iso(now),
                     log_url=d.log_url, env_url=d.env_url)
        return state

    async def _open(self, repo: str, env: str, key: str, verdict: EnvVerdict,
                    mode: str, config: dict, now: datetime, states: dict[str, dict]) -> None:
        d = verdict.newest
        state = self._measure({
            "first_sha": d.sha, "first_seen_at": _iso(now), "issue_number": None,
            "filed_at": None, "last_commented_at": None, "last_commented_sha": None,
            "issue_closed": False, "closed_sha": None, "recovery_commented": False,
            "notified": False, "muted": False, "regression_of": None,
            "vanished_noted": False,
        }, verdict, now)
        await self._ctx.set_target_state(key, state)
        streak = f" ({verdict.streak} in a row)" if verdict.streak > 1 else ""
        summary = f"{repo} · {env} deploy {verdict.kind} @ {_short(d.sha)}{streak}"
        # Informational, not "warn": ``notify_deploy_failed`` below is the actual
        # alert (deploy_failed). Emitting this at warn too would double-push a
        # wildcard notification rule (it also routes through automation_warn).
        await self._ctx.emit("info", summary, kind="incident", target=key,
                             detail={"sha": d.sha, "kind": verdict.kind, "streak": verdict.streak,
                                     "logUrl": d.log_url})
        await self._notify_once(repo, key, state, summary)
        if mode == "file":
            await self._file(repo, env, key, state, config, now, regression_of=None, states=states)

    async def _ongoing(self, repo: str, env: str, key: str, state: dict, verdict: EnvVerdict,
                       mode: str, config: dict, now: datetime, states: dict[str, dict]) -> None:
        d = verdict.newest
        self._measure(state, verdict, now)
        await self._ctx.set_target_state(key, state)
        # A muted row gets measurements only — no comment, no notify (spec §5.3
        # last row). Without this guard a row whose first notify failed at
        # ``_open`` (``notified`` still False) would fire the alert the tick it
        # gets muted, even though muting exists to silence exactly this.
        if not state.get("notified") and not state.get("muted"):
            summary = f"{repo} · {env} deploy {verdict.kind} @ {_short(d.sha)}"
            await self._notify_once(repo, key, state, summary)
        if mode != "file" or state.get("muted"):
            return
        if state.get("issue_number") is None:
            # A retried claim (the previous create failed) may already be a
            # regression — read the link back off the row rather than passing
            # None, or a failed regression create's link is lost on retry (m1).
            await self._file(repo, env, key, state, config, now,
                             regression_of=state.get("regression_of"), states=states)
            return
        if state.get("issue_closed"):
            if d.sha != state.get("closed_sha"):
                await self._file(repo, env, key, state, config, now,
                                 regression_of=int(state["issue_number"]), states=states)
            return
        last_sha = state.get("last_commented_sha") or state.get("first_sha")
        if d.sha == last_sha:
            return
        since = parse_iso(state.get("last_commented_at")) or parse_iso(state.get("filed_at"))
        linked = Linked(number=int(state["issue_number"]), closed=False, last_commented_at=since)
        decision = decide(linked, cap_remaining=1,
                          comment_every=timedelta(hours=int(config.get("comment_every_h", 6))), now=now)
        if decision.kind != "comment":
            return
        applied = await apply(decision, repo=repo, title="", body=recurrence_comment(state),
                              labels=(), linked=linked, run=self._run)
        if not applied.commented:
            return  # retried next tick; last_commented_* only move on success
        state["last_commented_at"] = _iso(now)
        state["last_commented_sha"] = d.sha
        await self._ctx.set_target_state(key, state)
        await self._ctx.emit("info", f"commented on #{linked.number}: {env} still {verdict.kind} "
                             f"@ {_short(d.sha)}", kind="commented", target=key)

    async def _recover(self, repo: str, env: str, key: str, state: dict, verdict: EnvVerdict,
                       now: datetime) -> None:
        """Close whatever issue is linked to this row, regardless of mode: the
        issue may have been opened by the loop in file mode, or filed by an
        operator's "File issue" card action while in watch mode. Either way an
        issue that exists is not left open once the environment recovers —
        only OPENING an issue from the loop is file-mode-only."""
        sha = verdict.newest.sha
        number = state.get("issue_number")
        if number and not state.get("issue_closed"):
            if not state.get("recovery_commented"):
                await asyncio.to_thread(ci_issues.comment_on_issue, repo, int(number),
                                        recovery_comment(env, sha, now), run=self._run)
                state["recovery_commented"] = True
                await self._ctx.set_target_state(key, state)
            ok = await asyncio.to_thread(ci_issues.close_issue, repo, int(number), run=self._run)
            if not ok:
                await self._ctx.emit("warn", f"could not close #{number} for {key}; will retry",
                                     kind="close_failed", target=key)
                return
            await self._ctx.emit("info", f"closed #{number}: {env} recovered @ {_short(sha)}",
                                 kind="closed", target=key,
                                 detail={"url": ci_issues.issue_url(repo, int(number))})
        await self._ctx.delete_target_state(key)
        await self._ctx.emit("info", f"{repo} · {env} recovered @ {_short(sha)}",
                             kind="recovered", target=key, detail={"sha": sha})

    # -- side effects ----------------------------------------------------------
    async def _notify_once(self, repo: str, key: str, state: dict, summary: str) -> None:
        if state.get("notified"):
            return
        try:
            await self._notify(f"{brand.NAME} — deploy {state.get('kind', 'failing')}", summary,
                               repo=repo, url=state.get("log_url"))
        except Exception:
            log.debug("deploy_monitor.notify_failed key=%s", key, exc_info=True)
            return
        state["notified"] = True
        await self._ctx.set_target_state(key, state)

    async def _file(self, repo: str, env: str, key: str, state: dict, config: dict,
                    now: datetime, *, regression_of: int | None, states: dict[str, dict]) -> int | None:
        """Two-phase like Error Triage: claim the row (``filed_at`` set,
        ``issue_number`` None) before the gh call, write the number after. A
        failed create leaves the row retryable — and, same as a retried Error
        Triage row, its ``filed_at`` still counts toward today's cap.

        The daily cap is derived from persisted ``filed_at`` on live rows
        (``states``, this tick's snapshot) rather than an in-memory counter: the
        daemon restarts on every rollout, and an in-memory counter would re-arm
        the cap on restart (Error Triage precedent, error_triage.py). ``states``
        is updated in place once a row is claimed so envs processed later in the
        SAME tick still see it. THIS row (``key``) is excluded from the count —
        a retried claim re-stamps its own ``filed_at`` every attempt, and Error
        Triage retries "outside the cap"; without the exclusion a retry at the
        cap's last slot could never succeed (it would always count itself)."""
        filed_today = sum(1 for k, s in states.items() if k != key
                          and (ts := parse_iso(s.get("filed_at"))) and now - ts < _DAY)
        cap_remaining = int(config.get("daily_cap", 10)) - filed_today
        decision = decide(None, cap_remaining=cap_remaining,
                          comment_every=timedelta(hours=1), now=now)
        if decision.kind != "file":
            if not state.get("cap_noted"):
                state["cap_noted"] = True
                await self._ctx.set_target_state(key, state)
                await self._ctx.emit("info", f"daily issue cap reached; {key} not filed",
                                     kind="cap", target=key)
            return None
        if regression_of is not None:
            decision = Decision("regress", "closed")
        state.update(filed_at=_iso(now), issue_number=None, issue_closed=False, closed_sha=None,
                     last_commented_at=None, last_commented_sha=None, recovery_commented=False,
                     cap_noted=False, regression_of=regression_of)
        await self._ctx.set_target_state(key, state)
        states[key] = state
        title = issue_title(repo, env, state)
        applied = await apply(decision, repo=repo, title=title,
                              body=issue_body(repo, env, state, regression_of=regression_of),
                              labels=[LABEL], linked=None, run=self._run)
        if applied.issue_number is None:
            await self._ctx.emit("warn", f"issue create failed — will retry: {title}",
                                 kind="file_failed", target=key)
            return None
        state["issue_number"] = applied.issue_number
        state["last_commented_sha"] = state.get("sha")
        await self._ctx.set_target_state(key, state)
        kind = "regressed" if regression_of is not None else "filed"
        await self._ctx.emit("info", f"filed #{applied.issue_number}: {title}", kind=kind, target=key,
                             detail={"url": ci_issues.issue_url(repo, applied.issue_number)})
        return applied.issue_number

    def _stash_counts(self, repos: int, envs: int, states: dict[str, dict], *,
                      degraded: bool = False) -> None:
        # A muted row is excluded from failing/stalled (it must not keep the card
        # red/yellow) but still counts toward the total — an operator silenced it,
        # it did not disappear.
        failing = sum(1 for s in states.values()
                      if s.get("kind") == "failing" and not s.get("muted"))
        stalled = sum(1 for s in states.values()
                      if s.get("kind") == "stalled" and not s.get("muted"))
        muted = sum(1 for s in states.values() if s.get("muted"))
        publish_observation(self._ctx, "deploy_monitor_counts", {"repos": repos, "envs": envs, "failing": failing,
                                           "stalled": stalled, "muted": muted,
                                           "incidents": failing + stalled + muted}, degraded=degraded)

    # -- card actions ----------------------------------------------------------
    async def actionable_items(self, limit: int = 50) -> list[ActionableItem]:
        mode, _ = await self._read_config()
        states = await self._ctx.list_target_states()
        items: list[ActionableItem] = []
        for key, s in sorted(states.items(), key=lambda kv: str(kv[1].get("first_seen_at") or "")):
            repo, env = split_key(key)
            number = s.get("issue_number")
            open_issue = bool(number) and not s.get("issue_closed")
            url = ci_issues.issue_url(repo, int(number)) if open_issue else s.get("log_url")
            subtitle = (f"@ {_short(s.get('sha'))} · {int(s.get('streak') or 0)}× · "
                        f"since {_fmt(s.get('first_seen_at'))}")
            if s.get("muted"):
                actions = [ActionSpec(kind="unmute", label="Unmute")]
            else:
                actions = [ActionSpec(kind="mute", label="Mute", style="risky")]
                if not open_issue and mode != "off":
                    actions.insert(0, ActionSpec(kind="file_now", label="File issue"))
            actions.append(ActionSpec(kind="dismiss", label="Dismiss", style="risky"))
            items.append(ActionableItem(id=key, title=f"{repo} · {env} {s.get('kind', 'failing')}",
                                        subtitle=subtitle, url=url, actions=actions))
            if len(items) >= limit:
                break
        return items

    async def action_result(self, key: str, kind: str) -> tuple[str, int | None]:
        """Returns ``(status, issue_number_filed_by_this_call)``."""
        mode, config = await self._read_config()
        state = await self._ctx.get_target_state(key)
        if not state:
            return "not_found", None
        now = self._clock()
        if kind == "mute":
            state["muted"] = True
            await self._ctx.set_target_state(key, state)
            await self._ctx.emit("info", f"{key} muted by operator", kind="muted", target=key)
            return "muted", None
        if kind == "unmute":
            state["muted"] = False
            await self._ctx.set_target_state(key, state)
            await self._ctx.emit("info", f"{key} unmuted", kind="unmuted", target=key)
            return "unmuted", None
        repo, env = split_key(key)
        if kind == "dismiss":
            number = state.get("issue_number")
            if number and not state.get("issue_closed"):
                await asyncio.to_thread(ci_issues.comment_on_issue, repo, int(number),
                                        dismiss_comment(env), run=self._run)
                await asyncio.to_thread(ci_issues.close_issue, repo, int(number), run=self._run)
            await self._ctx.delete_target_state(key)
            await self._ctx.emit("info", f"{repo} · {env} incident dismissed by operator",
                                 kind="dismissed", target=key)
            return "dismissed", None
        if kind != "file_now":
            return "failed", None
        if mode == "off":
            return "off", None
        if state.get("muted"):
            return "failed", None   # not offered on a muted row — mute/unmute/dismiss only
        if state.get("issue_number") and not state.get("issue_closed"):
            return "failed", None
        regression_of = int(state["issue_number"]) if state.get("issue_number") else None
        states = await self._ctx.list_target_states()
        number = await self._file(repo, env, key, state, {**config, "daily_cap": 10 ** 6}, now,
                                  regression_of=regression_of, states=states)
        return ("filed", number) if number is not None else ("failed", None)


# ---------------------------------------------------------------------------
# The action provider
# ---------------------------------------------------------------------------

_ACTION_STATUS = {
    "filed": ("ok", "Issue filed"),
    "muted": ("ok", "Muted — no comments or alerts until it recovers"),
    "unmuted": ("ok", "Unmuted"),
    "dismissed": ("ok", "Incident dismissed"),
    "not_found": ("warn", "That incident no longer exists"),
    "off": ("warn", "Deploy Monitor is off — enable it first"),
    "failed": ("error", "Action failed — see the feed"),
}


class DeployMonitorActionProvider:
    """Card actions for deploy-monitor. Reads the loop off the orchestrator lazily —
    the providers dict is built before the loops are (orchestrator.__init__ order)."""

    valid_kinds = frozenset({"file_now", "mute", "unmute", "dismiss"})

    def __init__(self, orch: Any) -> None:
        self._orch = orch

    @property
    def _loop(self) -> "DeployMonitorLoop":
        return self._orch._deploy_monitor_loop

    async def actionable(self) -> list[ActionableItem]:
        return await self._loop.actionable_items()

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        status, number = await self._loop.action_result(item_id, kind)
        level, message = _ACTION_STATUS.get(status, ("error", "Action failed — see the feed"))
        if status == "filed" and number is not None:
            message = f"Issue filed: #{number}"
        return TriggerResult(status=status, level=level, message=message)
