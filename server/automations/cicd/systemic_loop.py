"""SystemicRedLoop: the systemic-red incident state machine (spec 2026-10-31 §3-§6).

Per tick and per repo it reads merge_group candidates and the latest push:main runs,
clears or renews live incidents, opens new ones (issue + one push), and in ``correct``
mode sets the issue-borne brake and dispatches one fix session per incident. It also
raises the queue-thrash finding and bridges AWS spend alarms. Every I/O seam is
injected. State lives in the ctx's target-state rows and is written after each
successful side effect, so a restart never replays one. Fail-open: an unreadable repo
changes nothing, and a tick never raises.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from ..base import ActionableItem, ActionSpec
from ..context import observation_failed
from . import systemic as sy
from .systemic import RunObs

log = logging.getLogger(__name__)

BRAKE_LABEL = "ci:systemic-red"
ISSUE_LABELS = ["ci-doctor", "ci-systemic"]
_DAY_S = 24 * 3600.0
_CONSUMER_CAP_S = 12 * 3600.0  # the KinDash consumer rejects markers further out
_ALARM_EVERY_S = 300.0
DEFAULTS: dict[str, Any] = {
    "systemic_window_min": 60, "systemic_min_prs": 3, "systemic_rollup_jobs": "ci,ci-gate",
    "systemic_external_feed_jobs": "^security: audit$", "systemic_quiet_clear_min": 90,
    "systemic_brake_ttl_min": 240, "systemic_brake_max_min": 600,
    "systemic_dispatch_fix": True, "systemic_brake": True,
    "systemic_max_fix_sessions_per_day": 3, "thrash_min_failed_per_hour": 6,
}


def _cfg(config: dict, key: str) -> Any:
    value = config.get(key)
    return DEFAULTS[key] if value is None or value == "" else value


def _short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


class SystemicRedLoop:
    def __init__(self, ctx: Any, *, read_config, list_repos,
                 fetch_runs: Callable[[str, str, float, str | None], Awaitable[list[RunObs]]],
                 fetch_log: Callable[[str, int], Awaitable[str]],
                 file_issue: Callable[[str, str, str, list[str]], Awaitable[int | None]],
                 comment_issue: Callable[[str, int, str], Awaitable[bool]],
                 edit_issue_body: Callable[[str, int, str], Awaitable[bool]],
                 add_label: Callable[[str, int, str], Awaitable[bool]],
                 remove_label: Callable[[str, int, str], Awaitable[bool]],
                 close_issue: Callable[[str, int], Awaitable[bool]],
                 issue_snapshot: Callable[[str, int], Awaitable[dict | None]],
                 notify: Callable[..., Awaitable[None]],
                 spawn_fix: Callable[[str, str, str], Awaitable[str | None]],
                 failed_hourly_counts: Callable[[str], Awaitable[tuple[int, list[int], float, int]]],
                 alarms_in_alarm: Callable[[dict], Awaitable[list[dict] | None]],
                 clock: Callable[[], float]) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch_runs = fetch_runs
        self._fetch_log = fetch_log
        self._file_issue = file_issue
        self._comment = comment_issue
        self._edit = edit_issue_body
        self._add_label = add_label
        self._remove_label = remove_label
        self._close = close_issue
        self._snapshot = issue_snapshot
        self._notify = notify
        self._spawn = spawn_fix
        self._hourly = failed_hourly_counts
        self._alarms = alarms_in_alarm
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_alarm_check: float | None = None
        self._failed_hour: dict[str, int] = {}  # repo -> failed merge_group runs, last hour

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # noqa: BLE001 — a tick must never crash the loop
                observation_failed(self._ctx)
                log.warning("cicd_systemic.tick_failed", exc_info=True)

    async def _safe(self, fn: Callable[..., Awaitable[Any]], *args: Any, default: Any = None,
                    **kw: Any) -> Any:
        """Call one seam; any exception is logged and read as ``default`` (fail-open)."""
        try:
            return await fn(*args, **kw)
        except Exception:  # noqa: BLE001
            log.warning("cicd_systemic.seam_failed %s", getattr(fn, "__name__", fn), exc_info=True)
            return default

    async def _save(self, key: str, state: dict) -> None:
        await self._ctx.set_target_state(key, state)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            await self._lift_all()
            self._ctx.cicd_systemic_counts = {"incidents": 0, "braked": 0, "thrash": False,
                                              "alarms": 0}
            return
        now = self._clock()
        for repo in await self._list_repos(config):
            try:
                await self._repo_tick(str(repo), mode, config, now)
            except Exception:  # noqa: BLE001 — one repo must never stall the tick
                log.warning("cicd_systemic.repo_failed repo=%s", repo, exc_info=True)
        await self._aws(config, now)
        states = await self._ctx.list_target_states()
        incidents = [s for k, s in states.items() if k.startswith("systemic:")]
        self._ctx.cicd_systemic_counts = {
            "incidents": len(incidents),
            "braked": sum(1 for s in incidents if s.get("brake_state") == "set"),
            "thrash": any(k.startswith("systemic-thrash:") for k in states),
            "alarms": sum(1 for k in states if k.startswith("systemic-alarm:")),
        }

    async def _lift_all(self) -> None:
        """Off mode: lift every live brake now. No fetching, detection or alerts."""
        for key, state in (await self._ctx.list_target_states()).items():
            if key.startswith("systemic:") and state.get("brake_state") in ("set", "failed"):
                repo = key[len("systemic:"):].rsplit(":", 1)[0]
                try:
                    await self._lift(repo, key, state, "lifted")
                except Exception:  # noqa: BLE001
                    log.warning("cicd_systemic.lift_failed key=%s", key, exc_info=True)

    # --- per repo -----------------------------------------------------------------

    async def _repo_tick(self, repo: str, mode: str, config: dict, now: float) -> None:
        window_s = float(_cfg(config, "systemic_window_min")) * 60
        quiet_s = float(_cfg(config, "systemic_quiet_clear_min")) * 60
        try:  # both reads first: a failed read changes nothing this tick
            cands = await self._fetch_runs(repo, "merge_group", now - max(window_s, quiet_s), None)
            pushes = await self._fetch_runs(repo, "push", now - _DAY_S, "main")
        except Exception:  # noqa: BLE001
            log.warning("cicd_systemic.fetch_failed repo=%s", repo, exc_info=True)
            return
        mains: dict[int, RunObs] = {}
        for r in pushes:
            if r.status == "completed" and (r.workflow_id not in mains
                                            or r.created_ts > mains[r.workflow_id].created_ts):
                mains[r.workflow_id] = r
        windowed = [c for c in cands if now - window_s < c.created_ts <= now]
        rollups = frozenset(j.strip() for j in str(_cfg(config, "systemic_rollup_jobs")).split(",")
                            if j.strip())
        await self._thrash(repo, cands, config, now)
        prefix = f"systemic:{repo}:"
        live = {k: s for k, s in (await self._ctx.list_target_states()).items()
                if k.startswith(prefix)}
        hits = {h.job: h for h in sy.detect(windowed, mains, now=now, window_s=window_s,
                                            min_prs=int(_cfg(config, "systemic_min_prs")),
                                            rollups=rollups)}
        for key, state in live.items():
            if state.get("clearing") or sy.is_cleared(
                    state["job"], float(state["opened_ts"]), cands, mains, now=now,
                    quiet_s=quiet_s, rollups=rollups):
                await self._clear(repo, key, state)
                continue
            if state.get("dismissed"):
                continue  # operator dismissed: tracked only so it can clear, never acted on
            hit = hits.get(state["job"])
            if hit is not None:
                await self._reclassify(key, state, hit, windowed, mains, config)
            await self._live(repo, key, state, mode, config, now)
        live_jobs = {s.get("job") for s in live.values()}
        for job, hit in hits.items():
            if job not in live_jobs:
                await self._open(repo, prefix, hit, windowed, mains, mode, config, now)

    async def _reclassify(self, key: str, state: dict, hit: sy.Hit, cands: list[RunObs],
                          mains: dict[int, RunObs], config: dict) -> None:
        """§5c: re-run the pure classifier; a move out of BRAKE_CLASSES lifts the brake
        (in ``_correct``) and the incident is never re-braked or re-dispatched."""
        klass = sy.classify(hit, cands, mains, normalized_sig=state.get("signature") or "",
                            external_feed_re=str(_cfg(config, "systemic_external_feed_jobs")))
        if klass == state.get("class"):
            return
        state["class"] = klass
        if klass == "poisoner":
            state["poisoner_pr"] = sy.poisoner_pr(hit, cands, mains)
        if klass not in sy.BRAKE_CLASSES:
            state["reclassified_out"] = True
        await self._save(key, state)
        await self._ctx.emit("info", f"{state['job']} reclassified as {sy.class_label(klass)}",
                             kind="systemic_reclassified", target=key)

    async def _live(self, repo: str, key: str, state: dict, mode: str, config: dict,
                    now: float) -> None:
        if state.get("owner_closed"):
            return
        number = state.get("issue_number")
        if number is None:
            await self._file(repo, key, state)
        elif (await self._safe(self._snapshot, repo, number) or {}).get("state") == "closed":
            state["owner_closed"] = True
            if state.get("brake_state") in ("set", "failed"):
                state["brake_state"] = "lifted"
            await self._save(key, state)
            await self._ctx.emit("info", f"{repo} #{number} closed by a human; brake lifted, "
                                 "incident left to clear on its own", kind="systemic_owner_closed",
                                 target=key)
            return
        await self._correct(repo, key, state, mode, config, now)
        await self._alert_open(repo, key, state)

    async def _open(self, repo: str, prefix: str, hit: sy.Hit, cands: list[RunObs],
                    mains: dict[int, RunObs], mode: str, config: dict, now: float) -> None:
        log_text = ""
        if hit.failing_job_ids:
            log_text = await self._safe(self._fetch_log, repo, hit.failing_job_ids[-1],
                                        default="") or ""
        sig, excerpt = sy.signature(log_text)
        fp = sy.fingerprint(repo, hit.job, sig)
        klass = sy.classify(hit, cands, mains, normalized_sig=sig,
                            external_feed_re=str(_cfg(config, "systemic_external_feed_jobs")))
        state: dict[str, Any] = {
            "job": hit.job, "class": klass, "rule": hit.rule, "fingerprint": fp,
            "opened_ts": now, "first_failed_ts": hit.first_failed_ts,
            "last_failed_ts": hit.last_failed_ts, "prs": list(hit.prs),
            "runs": list(hit.failing_runs), "signature": sig, "excerpt": excerpt,
            "poisoner_pr": sy.poisoner_pr(hit, cands, mains) if klass == "poisoner" else None,
            "issue_number": None, "brake_set_ts": None, "brake_expires_ts": None,
            "brake_state": "none", "session_id": None, "alerted_open": False,
            "owner_closed": False, "failed_per_hour": self._failed_hour.get(repo),
        }
        key = prefix + fp
        await self._save(key, state)
        await self._ctx.emit("warn", f"{repo}: {hit.job} systemic red on {len(hit.prs)} PRs "
                             f"({sy.class_label(klass)})", kind="systemic_open", target=key)
        await self._file(repo, key, state)
        await self._correct(repo, key, state, mode, config, now)
        await self._alert_open(repo, key, state)

    async def _file(self, repo: str, key: str, state: dict) -> None:
        number = await self._safe(self._file_issue, repo, sy.issue_title(state),
                                  sy.issue_body(state, repo=repo), list(ISSUE_LABELS))
        if number is None:
            await self._ctx.emit("warn", f"{repo}: incident issue for {state['job']} not filed; "
                                 "will retry", kind="systemic_file_failed", target=key)
            return
        state["issue_number"] = int(number)
        await self._save(key, state)

    async def _alert_open(self, repo: str, key: str, state: dict) -> None:
        if state.get("alerted_open"):
            return
        brake = {"set": ", queue braked", "failed": ", brake NOT set"}.get(
            state.get("brake_state", ""), "")
        title = (f"{_short(repo)}: {state['job']} red on {len(state.get('prs') or ())} PRs "
                 f"({sy.class_label(state['class'])}{brake})")
        body = sy.recommended_action(state)
        state["alerted_open"] = True  # persisted first: a crash can't double-push
        await self._save(key, state)
        await self._safe(self._notify, "ci_systemic_red", title, body, repo=repo,
                         url=self._issue_url(repo, state))

    @staticmethod
    def _issue_url(repo: str, state: dict) -> str | None:
        n = state.get("issue_number")
        return f"https://github.com/{repo}/issues/{n}" if n else None

    # --- correct mode: brake and fix dispatch -------------------------------------

    async def _correct(self, repo: str, key: str, state: dict, mode: str, config: dict,
                       now: float) -> None:
        brake_class = state.get("class") in sy.BRAKE_CLASSES
        if (mode == "correct" and brake_class and not state.get("reclassified_out")
                and bool(_cfg(config, "systemic_brake"))):
            if state.get("issue_number") is not None:
                await self._brake(repo, key, state, config, now)
        elif state.get("brake_state") in ("set", "failed"):
            # Braking turned off (mode, knob, or the class left BRAKE_CLASSES): lift now.
            await self._lift(repo, key, state, "lifted")
        if (mode == "correct" and brake_class and not state.get("reclassified_out")
                and bool(_cfg(config, "systemic_dispatch_fix")) and not state.get("session_id")):
            await self._dispatch(repo, key, state, config, now)

    async def _lift(self, repo: str, key: str, state: dict, final: str) -> bool:
        """Remove the label and marker, then record ``final`` ("lifted" | "ceiling").
        On a failed write the state is unchanged, so the next tick retries."""
        number = state.get("issue_number")
        if number is not None and not await self._unbrake(repo, int(number),
                                                          {**state, "brake_state": final}):
            await self._ctx.emit("warn", f"{repo} #{number}: brake lift failed; will retry",
                                 kind="systemic_lift_failed", target=key)
            return False
        state["brake_state"] = final
        await self._save(key, state)
        return True

    async def _brake(self, repo: str, key: str, state: dict, config: dict, now: float) -> None:
        if state.get("brake_state") in ("lifted", "ceiling"):
            return  # never re-brake an incident once lifted
        number = int(state["issue_number"])
        max_s = float(_cfg(config, "systemic_brake_max_min")) * 60
        set_ts = state.get("brake_set_ts")
        if set_ts is not None and now - float(set_ts) >= max_s:
            if not await self._lift(repo, key, state, "ceiling"):
                return
            hours = max_s / 3600
            await self._safe(self._notify, "ci_systemic_red",
                             f"{_short(repo)}: brake lifted after {hours:g} h; incident still live",
                             f"{state['job']} is still red; the brake will not be re-applied.",
                             repo=repo, url=self._issue_url(repo, state))
            return
        anchor = float(set_ts) if set_ts is not None else now
        ttl_s = float(_cfg(config, "systemic_brake_ttl_min")) * 60
        expires = min(now + ttl_s, anchor + max_s, now + _CONSUMER_CAP_S)
        marker = sy.brake_marker(job=state["job"], klass=state["class"],
                                 incident=state["fingerprint"], set_at=anchor, expires_at=expires)
        state["failed_per_hour"] = self._failed_hour.get(repo, state.get("failed_per_hour"))
        snap = await self._safe(self._snapshot, repo, number)
        ok = False
        if snap is not None:  # a failed read never opens or renews a brake
            shown = {**state, "brake_state": "set", "brake_expires_ts": expires}
            body = sy.issue_body(shown, repo=repo) + "\n\n" + marker
            if BRAKE_LABEL not in (snap.get("labels") or []):
                await self._safe(self._add_label, repo, number, BRAKE_LABEL, default=False)
            if await self._safe(self._edit, repo, number, body, default=False):
                check = await self._safe(self._snapshot, repo, number)
                ok = bool(check) and marker in (check.get("body") or "") \
                    and BRAKE_LABEL in (check.get("labels") or [])
        if ok:
            state.update(brake_state="set", brake_set_ts=anchor, brake_expires_ts=expires)
        else:
            state["brake_state"] = "failed"
            await self._ctx.emit("warn", f"{repo} #{number}: brake NOT set for {state['job']}; "
                                 "will retry next tick", kind="systemic_brake_failed", target=key)
        await self._save(key, state)

    async def _unbrake(self, repo: str, number: int, shown: dict) -> bool:
        """Make the issue carry no brake. Label absent and marker absent is success; a
        present marker is replaced by the body rebuilt from ``shown``."""
        snap = await self._safe(self._snapshot, repo, number)
        if snap is None:
            return False
        if BRAKE_LABEL in (snap.get("labels") or []) and not await self._safe(
                self._remove_label, repo, number, BRAKE_LABEL, default=False):
            return False
        body = snap.get("body") or ""
        if sy.strip_brake_marker(body) == body:
            return True
        return bool(await self._safe(self._edit, repo, number, sy.issue_body(shown, repo=repo),
                                     default=False))

    async def _dispatch(self, repo: str, key: str, state: dict, config: dict, now: float) -> None:
        day_key = f"systemic-dispatch:{repo}:{time.strftime('%Y-%m-%d', time.gmtime(now))}"
        counter = await self._ctx.get_target_state(day_key) or {}
        count = int(counter.get("count", 0))
        if count >= int(_cfg(config, "systemic_max_fix_sessions_per_day")):
            return
        sid = await self._safe(self._spawn, repo, f"{repo}#systemic:{state['fingerprint']}",
                               sy.fix_prompt(state, repo=repo))
        if not sid:
            return  # not spawned: retry next tick, counter untouched
        state["session_id"] = str(sid)
        await self._save(key, state)
        await self._ctx.set_target_state(day_key, {"count": count + 1})
        await self._ctx.emit("info", f"{repo}: fix session {sid} dispatched for {state['job']}",
                             kind="systemic_dispatch", target=key, session_id=str(sid))

    # --- card actions (spec §5a) ---------------------------------------------------

    async def actionable_items(self) -> list[ActionableItem]:
        """One row per live incident: Clear brake while a brake is (or may be) on the
        issue, Dismiss always."""
        items: list[ActionableItem] = []
        for key, st in sorted((await self._ctx.list_target_states()).items()):
            if not key.startswith("systemic:") or st.get("clearing") or st.get("dismissed"):
                continue
            repo = key[len("systemic:"):].rsplit(":", 1)[0]
            brake = st.get("brake_state")
            actions = []
            if brake in ("set", "failed"):
                actions.append(ActionSpec(kind="clear_brake", label="Clear brake",
                                          style="risky"))
            actions.append(ActionSpec(kind="dismiss", label="Dismiss", style="risky"))
            items.append(ActionableItem(
                id=key, subtitle=repo, url=self._issue_url(repo, st), actions=actions,
                title=(f"{st.get('job')} red on {len(st.get('prs') or ())} PRs "
                       f"({sy.class_label(str(st.get('class')))})"),
                status={"set": "Queue braked", "failed": "Brake NOT set",
                        "lifted": "Brake lifted", "ceiling": "Brake lifted at ceiling"
                        }.get(str(brake))))
        return items

    async def action_result(self, key: str, kind: str) -> str:
        """``clear_brake``: lift the brake now and record ``lifted``, so the incident is
        never re-braked. ``dismiss``: lift, close the issue with a dismiss comment and
        keep the state marked ``dismissed``, so a still-red job never re-opens; it takes
        no further side effects and is dropped silently when the job clears.
        Returns brake_cleared | dismissed | not_found | failed."""
        async with self._lock:  # never interleave with a tick's writes
            state = await self._ctx.get_target_state(key) if key.startswith("systemic:") \
                else None
            if not state:
                return "not_found"
            if state.get("dismissed"):
                return "dismissed" if kind == "dismiss" else "not_found"
            if kind not in ("clear_brake", "dismiss"):
                return "failed"
            repo = key[len("systemic:"):].rsplit(":", 1)[0]
            if not await self._lift(repo, key, state, "lifted"):
                return "failed"
            if kind == "clear_brake":
                await self._ctx.emit("info", f"{repo}: brake cleared for {state['job']} by "
                                     "the operator", kind="systemic_brake_cleared", target=key)
                return "brake_cleared"
            number = state.get("issue_number")
            if number is not None and not state.get("owner_closed"):
                if not state.get("dismiss_commented") and await self._safe(
                        self._comment, repo, int(number),
                        f"Dismissed by the operator from ViberPit; `{state['job']}` is no "
                        "longer tracked as a systemic incident. Closing.", default=False):
                    state["dismiss_commented"] = True  # a retried dismiss never re-comments
                    await self._save(key, state)
                if not await self._safe(self._close, repo, int(number), default=False):
                    return "failed"  # not yet dismissed (brake lifted): a retry re-closes
            state["dismissed"] = True
            await self._save(key, state)
            await self._ctx.emit("info", f"{repo}: {state['job']} incident dismissed by the "
                                 "operator", kind="systemic_dismissed", target=key)
            return "dismissed"

    # --- clearing -----------------------------------------------------------------

    async def _clear(self, repo: str, key: str, state: dict) -> None:
        """Lift, comment, close, notify, forget; each step journaled so none replays.
        A dismissed incident already had its issue closed: it is dropped silently."""
        if state.get("dismissed"):
            await self._ctx.delete_target_state(key)
            await self._ctx.emit("info", f"{repo}: dismissed {state['job']} incident cleared",
                                 kind="systemic_cleared", target=key)
            return
        if not state.get("clearing"):
            state["clearing"] = True
            await self._save(key, state)
        number = state.get("issue_number")
        done = set(state.get("clear_done") or ())
        braked = state.get("brake_state") in ("set", "failed")
        if number is not None and not state.get("owner_closed"):
            lifted = " Brake lifted;" if braked and state.get("brake_set_ts") is not None else ""
            steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
                ("comment", lambda: self._safe(
                    self._comment, repo, number,
                    f"Recovered: `{state['job']}` ran green again.{lifted} Closing.",
                    default=False)),
                ("close", lambda: self._safe(self._close, repo, number, default=False)),
            ]
            if braked:
                steps.insert(0, ("unbrake", lambda: self._unbrake(
                    repo, int(number), {**state, "brake_state": "lifted"})))
            for name, step in steps:
                if name in done:
                    continue
                if not await step():
                    return  # retry the rest next tick
                done.add(name)
                state["clear_done"] = sorted(done)
                await self._save(key, state)
        if state.get("alerted_open") and not state.get("cleared_notified"):
            state["cleared_notified"] = True  # persisted first: a crash can't double-push
            await self._save(key, state)
            await self._safe(self._notify, "ci_systemic_red",
                             f"{_short(repo)}: {state['job']} cleared",
                             f"{state['job']} is green again; the incident is closed.",
                             repo=repo, url=self._issue_url(repo, state))
        await self._ctx.delete_target_state(key)
        await self._ctx.emit("info", f"{repo}: {state['job']} systemic red cleared",
                             kind="systemic_cleared", target=key)

    # --- thrash and AWS -----------------------------------------------------------

    async def _thrash(self, repo: str, cands: list[RunObs], config: dict, now: float) -> None:
        counts = await self._safe(self._hourly, repo)
        if counts is None:
            return
        failed, hourly, ratio, runs = counts
        self._failed_hour[repo] = int(failed)
        key = f"systemic-thrash:{repo}"
        on = sy.thrash(failed, list(hourly), float(ratio), int(runs),
                       min_failed=int(_cfg(config, "thrash_min_failed_per_hour")))
        seen = bool(await self._ctx.get_target_state(key))
        if not on:
            if seen:
                await self._ctx.delete_target_state(key)
            return
        if seen:
            return
        # Upper bound: job completion minus run creation (the jobs API start time is
        # not in RunObs), over failed candidates of the last hour.
        minutes = sum((j.completed_ts - r.created_ts) / 60 for r in cands
                      if r.conclusion == "failure" and r.created_ts > now - 3600
                      for j in r.jobs if j.completed_ts is not None)
        body = (f"{failed} failed merge_group runs in the last hour ({ratio:.0%} of {runs}); "
                f"about {minutes:.0f} runner-minutes (upper bound) burned.")
        await self._ctx.set_target_state(key, {"since": now, "failed": failed})
        await self._ctx.emit("warn", f"{repo}: queue thrash. {body}", kind="queue_thrash",
                             target=key)
        await self._safe(self._notify, "ci_systemic_red", f"{_short(repo)}: merge queue thrash",
                         body, repo=repo, url=None)

    async def _aws(self, config: dict, now: float) -> None:
        if self._last_alarm_check is not None and now - self._last_alarm_check < _ALARM_EVERY_S:
            return
        self._last_alarm_check = now
        alarms = await self._safe(self._alarms, config)
        if alarms is None:
            return  # a failed read forgets nothing
        names = {str(a.get("name")) for a in alarms if a.get("name")}
        states = await self._ctx.list_target_states()
        for key in states:
            if key.startswith("systemic-alarm:") and key.split(":", 1)[1] not in names:
                await self._ctx.delete_target_state(key)
                await self._ctx.emit("info", f"AWS alarm {key.split(':', 1)[1]} left ALARM",
                                     kind="spend_alarm_ok", target=key)
        for a in alarms:
            name = str(a.get("name") or "")
            key = f"systemic-alarm:{name}"
            if not name or key in states:
                continue
            body = f"{name} is in ALARM: {a.get('reason') or 'no reason given'}"
            await self._ctx.set_target_state(key, {"since": now})
            await self._ctx.emit("warn", body, kind="spend_alarm", target=key)
            await self._safe(self._notify, "ci_spend_alarm", f"AWS spend alarm: {name}", body,
                             repo=None, url=None)
