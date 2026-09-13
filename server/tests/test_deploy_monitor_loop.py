# server/tests/test_deploy_monitor_loop.py
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server.automations import deploy_monitor as dm
from server.github.deployments import Deployment

T0 = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
REPO = "cairnea/KinDash"
ADMIN = "kindash / admin"


class FakeCtx:
    """In-memory stand-in for AutomationContext: events + target state."""

    def __init__(self):
        self.events: list[dict] = []
        self.states: dict[str, dict] = {}

    async def emit(self, level, summary, *, kind=None, target=None, session_id=None, detail=None):
        self.events.append({"level": level, "summary": summary, "kind": kind,
                            "target": target, "detail": detail})

    async def get_target_state(self, target):
        return dict(self.states.get(target, {}))

    async def set_target_state(self, target, state):
        self.states[target] = dict(state)

    async def delete_target_state(self, target):
        self.states.pop(target, None)

    async def list_target_states(self):
        return {k: dict(v) for k, v in self.states.items()}

    def kinds(self):
        return [e["kind"] for e in self.events]


class FakeGh:
    """Records gh calls; answers issue creates with sequential numbers and
    issue_states GraphQL with ``self.closed``."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.next_number = 100
        self.closed: dict[int, str | None] = {}   # number -> stateReason ("completed"/"not_planned")
        self.fail_create = False

    def __call__(self, args, token=None):
        import json
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            nodes = {f"i{n}": {"number": n, "closed": True, "stateReason": r}
                     for n, r in self.closed.items()}
            return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"repository": nodes}}), stderr="")
        if args[0] == "api" and args[1].endswith("/issues") and "-X" not in args:
            if self.fail_create:
                return SimpleNamespace(returncode=1, stdout="", stderr="nope")
            self.next_number += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps({"number": self.next_number}), stderr="")
        if "/comments" in args[1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"html_url": "https://x/c/1"}), stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def creates(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/issues") and "-X" not in c]

    def comments(self):
        return [c for c in self.calls if len(c) > 1 and "/comments" in c[1]]

    def closes(self):
        return [c for c in self.calls if "-X" in c and "state=closed" in c]

    def labels(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/labels")]


def _dep(env=ADMIN, sha="a" * 40, state="FAILURE", age_min=10):
    return Deployment(env=env, sha=sha, ref="main", creator="railway-app",
                      created_at=T0 - timedelta(minutes=age_min + 2), status_state=state,
                      status_at=T0 - timedelta(minutes=age_min), log_url="https://railway.app/log",
                      env_url=None)


class Harness:
    def __init__(self, mode="file", config=None):
        self.ctx = FakeCtx()
        self.gh = FakeGh()
        self.mode = mode
        self.config = {"enabled_repos": [], "all_envs": False, "grace_min": 5, "stall_min": 30,
                       "comment_every_h": 6, "daily_cap": 10, **(config or {})}
        self.now = T0
        self.deployments: dict[str, list[Deployment] | None] = {REPO: [_dep()]}
        self.notified: list[dict] = []
        self.notify_fails = False

        async def read_config():
            return self.mode, dict(self.config)

        async def list_repos(config):
            return list(self.deployments.keys())

        async def fetch(repos):
            return {r: self.deployments.get(r) for r in repos}

        async def notify(title, body, *, repo, url):
            if self.notify_fails:
                raise RuntimeError("push down")
            self.notified.append({"title": title, "body": body, "repo": repo, "url": url})

        self.loop = dm.DeployMonitorLoop(self.ctx, read_config=read_config, list_repos=list_repos,
                                         fetch_deployments=fetch, run=self.gh, notify=notify,
                                         clock=lambda: self.now)

    def tick(self):
        asyncio.run(self.loop.tick())

    def state(self, env=ADMIN):
        return self.ctx.states.get(dm.incident_key(REPO, env))


KEY = dm.incident_key(REPO, ADMIN)


def test_new_incident_alerts_once_and_files_one_issue():
    h = Harness()
    h.tick()
    st = h.state()
    assert st and st["kind"] == "failing" and st["issue_number"] == 101 and st["notified"] is True
    assert len(h.notified) == 1 and h.notified[0]["repo"] == REPO
    assert h.notified[0]["url"] == "https://railway.app/log"
    assert len(h.gh.creates()) == 1 and any("labels[]=deploy-monitor" in c for c in h.gh.creates()[0])
    assert "incident" in h.ctx.kinds() and "filed" in h.ctx.kinds()
    assert h.ctx.deploy_monitor_counts == {"repos": 1, "envs": 1, "failing": 1, "stalled": 0,
                                           "muted": 0, "incidents": 1}
    h.tick()   # same sha: nothing new
    assert len(h.notified) == 1 and len(h.gh.creates()) == 1 and not h.gh.comments()


def test_watch_mode_alerts_and_opens_no_issue():
    h = Harness(mode="watch")
    h.tick()
    assert len(h.notified) == 1 and h.state()["issue_number"] is None
    assert not h.gh.calls


def test_off_mode_does_not_fabricate_observation():
    h = Harness(mode="off")
    h.tick()
    assert not h.ctx.states and not h.notified and not h.gh.calls
    assert not hasattr(h.ctx, "deploy_monitor_counts")
    assert not hasattr(h.ctx, "observed_at")


def test_unclassified_env_ignored_unless_all_envs():
    h = Harness()
    h.deployments = {REPO: [_dep(env="preview-7")]}
    h.tick()
    assert not h.ctx.states
    h.config["all_envs"] = True
    h.tick()
    assert h.state("preview-7")


def test_young_failure_inside_grace_opens_nothing_then_opens():
    h = Harness()
    h.deployments = {REPO: [_dep(age_min=1)]}
    h.tick()
    assert not h.ctx.states and not h.notified
    h.now = T0 + timedelta(minutes=10)
    h.tick()
    assert h.state()


def test_ongoing_comments_only_when_sha_moves_and_throttled():
    h = Harness()
    h.tick()
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep(sha="a" * 40)]}
    h.now = T0 + timedelta(minutes=10)
    h.tick()
    assert len(h.gh.comments()) == 0            # inside comment_every_h since filing
    h.now = T0 + timedelta(hours=7)
    h.tick()
    assert len(h.gh.comments()) == 1 and h.state()["last_commented_sha"] == "b" * 40
    assert h.state()["streak"] == 2
    h.now = T0 + timedelta(hours=20)
    h.tick()                                     # sha unchanged since last comment
    assert len(h.gh.comments()) == 1
    assert len(h.notified) == 1


def test_recovery_comments_closes_and_forgets():
    h = Harness()
    h.tick()
    h.deployments = {REPO: [_dep(sha="c" * 40, state="SUCCESS"), _dep()]}
    h.tick()
    assert h.state() is None
    assert len(h.gh.comments()) == 1 and len(h.gh.closes()) == 1
    assert "recovered" in h.ctx.kinds() and "closed" in h.ctx.kinds()
    assert h.ctx.deploy_monitor_counts["incidents"] == 0


def test_recovery_in_watch_mode_forgets_without_writing():
    h = Harness(mode="watch")
    h.tick()
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None and not h.gh.calls


def test_close_failure_keeps_row_and_retries_without_duplicate_comment():
    h = Harness()
    h.tick()
    close_calls = {"n": 0}
    real = h.gh.__call__

    def flaky(args, token=None):
        if "state=closed" in args:
            close_calls["n"] += 1
            if close_calls["n"] == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="503")
        return real(args, token=token)

    h.loop._run = flaky
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is not None and h.state()["recovery_commented"] is True
    h.tick()
    assert h.state() is None and close_calls["n"] == 2
    assert len(h.gh.comments()) == 1


def test_fetch_failure_changes_nothing_and_warns_once_per_outage():
    h = Harness()
    h.tick()
    h.deployments = {REPO: None}
    h.tick()
    h.tick()
    assert h.state()["issue_number"] == 101 and h.ctx.kinds().count("fetch_failed") == 1
    h.deployments = {REPO: [_dep()]}
    h.tick()
    assert "fetch_recovered" in h.ctx.kinds()


def test_failed_create_is_retried_next_tick():
    h = Harness()
    h.gh.fail_create = True
    h.tick()
    assert h.state()["issue_number"] is None and "file_failed" in h.ctx.kinds()
    h.gh.fail_create = False
    h.tick()
    assert h.state()["issue_number"] == 101 and len(h.gh.creates()) == 2


def test_daily_cap_blocks_new_issues_but_not_alerts():
    h = Harness(config={"daily_cap": 1})
    h.deployments = {REPO: [_dep(), _dep(env="kindash / qa", sha="q" * 40)]}
    h.tick()
    assert len(h.gh.creates()) == 1 and len(h.notified) == 2
    assert h.ctx.kinds().count("cap") == 1


def test_closed_not_planned_mutes_until_recovery():
    h = Harness()
    h.tick()
    h.gh.closed = {101: "not_planned"}
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep()]}
    h.now = T0 + timedelta(hours=8)
    h.tick()
    assert h.state()["muted"] is True and not h.gh.comments() and len(h.gh.creates()) == 1
    assert "muted" in h.ctx.kinds()
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None and not h.gh.closes()   # already closed by the human


def test_closed_completed_regresses_only_on_a_new_sha():
    h = Harness()
    h.tick()
    h.gh.closed = {101: "completed"}
    h.now = T0 + timedelta(hours=1)
    h.tick()                                   # same sha: acknowledged, no new issue
    assert h.state()["issue_closed"] is True and len(h.gh.creates()) == 1
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep()]}
    h.gh.closed = {}
    h.now = T0 + timedelta(hours=2)
    h.tick()
    assert len(h.gh.creates()) == 2 and h.state()["issue_number"] == 102
    assert h.state()["issue_closed"] is False
    assert any("regression" in e["summary"].lower() or e["kind"] == "regressed" for e in h.ctx.events)


def test_notify_failure_retries_next_tick():
    h = Harness(mode="watch")
    h.notify_fails = True
    h.tick()
    assert h.state()["notified"] is False
    h.notify_fails = False
    h.tick()
    assert h.state()["notified"] is True and len(h.notified) == 1


def test_stalled_then_failing_is_one_incident():
    h = Harness(mode="watch")
    h.deployments = {REPO: [_dep(state="IN_PROGRESS", age_min=40)]}
    h.tick()
    assert h.state()["kind"] == "stalled"
    h.deployments = {REPO: [_dep(state="FAILURE", age_min=50)]}
    h.tick()
    assert h.state()["kind"] == "failing" and len(h.notified) == 1
    assert h.ctx.kinds().count("incident") == 1


def test_actionable_items_and_actions():
    h = Harness(mode="watch")
    h.tick()
    items = asyncio.run(h.loop.actionable_items())
    assert len(items) == 1 and items[0].id == KEY
    assert {a.kind for a in items[0].actions} == {"file_now", "mute", "dismiss"}
    assert items[0].url == "https://railway.app/log"

    assert asyncio.run(h.loop.action_result(KEY, "mute")) == ("muted", None)
    items = asyncio.run(h.loop.actionable_items())
    assert {a.kind for a in items[0].actions} == {"unmute", "dismiss"}
    assert asyncio.run(h.loop.action_result(KEY, "unmute")) == ("unmuted", None)

    status, number = asyncio.run(h.loop.action_result(KEY, "file_now"))
    assert status == "filed" and number == 101
    items = asyncio.run(h.loop.actionable_items())
    assert {a.kind for a in items[0].actions} == {"mute", "dismiss"} and items[0].url.endswith("/issues/101")
    assert asyncio.run(h.loop.action_result("nope#x", "mute")) == ("not_found", None)
    h.mode = "off"
    assert asyncio.run(h.loop.action_result(KEY, "file_now")) == ("off", None)


def test_action_provider_maps_statuses():
    h = Harness(mode="watch")
    h.tick()
    orch = SimpleNamespace(_deploy_monitor_loop=h.loop)
    p = dm.DeployMonitorActionProvider(orch)
    assert p.valid_kinds == frozenset({"file_now", "mute", "unmute", "dismiss"})
    r = asyncio.run(p.trigger(KEY, "file_now"))
    assert r.status == "filed" and r.level == "ok" and "#101" in r.message
    r = asyncio.run(p.trigger("nope#x", "mute"))
    assert r.level == "warn"


def test_tick_never_raises(monkeypatch):
    h = Harness()

    async def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(h.loop, "_run_once", boom)
    h.tick()   # no exception


@pytest.mark.parametrize("bad", ["not-a-repo", "a/../b"])
def test_invalid_repo_is_skipped(bad):
    h = Harness()
    h.deployments = {bad: [_dep()]}
    h.tick()
    assert not h.ctx.states


# -- fix round 1 -------------------------------------------------------------

def test_muted_row_never_notifies_even_after_first_notify_failed():
    """Finding 1: a muted row gets measurements only — no notify, even when the
    FIRST notify (at ``_open``) failed and left ``notified`` False."""
    h = Harness(mode="watch")
    h.notify_fails = True
    h.tick()
    assert h.state()["notified"] is False
    assert asyncio.run(h.loop.action_result(KEY, "mute")) == ("muted", None)
    h.notify_fails = False
    h.tick()
    assert h.state()["notified"] is False and len(h.notified) == 0


def test_operator_filed_issue_closes_on_recovery_in_watch_mode():
    """Finding 2: an issue that EXISTS on a row — even one filed by the
    operator's ``file_now`` card action while in watch mode — is still closed
    with the recovery comment on recovery, regardless of mode."""
    h = Harness(mode="watch")
    h.tick()
    status, number = asyncio.run(h.loop.action_result(KEY, "file_now"))
    assert status == "filed" and number == 101
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None
    assert len(h.gh.comments()) == 1 and len(h.gh.closes()) == 1


def test_recovery_in_watch_mode_forgets_without_writing_when_no_issue():
    """Finding 2 companion: with no issue ever filed, recovery in watch mode
    still writes nothing to GitHub."""
    h = Harness(mode="watch")
    h.tick()
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None and not h.gh.calls


def test_operator_filed_issue_muted_when_closed_not_planned_in_watch_mode():
    """Finding 2: issue-state sync is a read, so it runs in watch mode too
    whenever a row holds an open issue — a human closing it not_planned mutes
    the row even though the loop is in watch mode."""
    h = Harness(mode="watch")
    h.tick()
    status, number = asyncio.run(h.loop.action_result(KEY, "file_now"))
    assert status == "filed" and number == 101
    h.gh.closed = {101: "not_planned"}
    h.now = T0 + timedelta(hours=1)
    h.tick()
    assert h.state()["muted"] is True
    assert "muted" in h.ctx.kinds()


def test_daily_cap_persists_across_a_simulated_restart():
    """Finding 3: the daily cap is derived from persisted ``filed_at`` on live
    rows, not an in-memory counter — a fresh ``DeployMonitorLoop`` instance
    (simulating a daemon restart) over the SAME ctx must not re-arm it."""
    h = Harness(config={"daily_cap": 1})
    h.deployments = {REPO: [_dep(), _dep(env="kindash / qa", sha="q" * 40)]}
    h.tick()
    assert len(h.gh.creates()) == 1
    restarted = dm.DeployMonitorLoop(h.ctx, read_config=h.loop._read_config,
                                     list_repos=h.loop._list_repos, fetch_deployments=h.loop._fetch,
                                     run=h.gh, notify=h.loop._notify, clock=lambda: h.now)
    asyncio.run(restarted.tick())
    assert len(h.gh.creates()) == 1


# -- fix round 2 (final review) -----------------------------------------------

def test_daily_cap_does_not_starve_a_capped_retry():
    """F1: ``filed_today`` used to sum over ALL live rows, including the row
    being filed. A row claimed by a failed create re-stamps its own ``filed_at``
    every attempt, so at the cap's last slot it counted itself and could never
    be retried."""
    h = Harness(config={"daily_cap": 1})
    h.gh.fail_create = True
    h.tick()
    assert h.state()["issue_number"] is None
    h.gh.fail_create = False
    h.tick()
    assert h.state()["issue_number"] == 101 and len(h.gh.creates()) == 2   # failed attempt + retry
    assert h.ctx.kinds().count("cap") == 0


def test_vanished_env_emits_once_then_clears_on_reappearance():
    """F2(a): a successful read that no longer contains the row's env (renamed
    service, rolled out of the deployment window) must not silently drop the
    incident — it is noted once and left for an operator, not auto-deleted."""
    h = Harness()
    h.tick()
    h.deployments = {REPO: []}   # successful read, but the env is gone from it
    h.tick()
    assert h.ctx.kinds().count("vanished") == 1
    assert h.state()["vanished_noted"] is True
    h.tick()   # still gone this tick — no duplicate
    assert h.ctx.kinds().count("vanished") == 1
    h.deployments = {REPO: [_dep()]}
    h.tick()
    assert h.state()["vanished_noted"] is False


def test_dismiss_closes_open_issue_and_deletes_row():
    """F2(b): dismiss is a best-effort close (comment + close) of whatever issue
    the row holds, then the row is gone regardless of mode."""
    h = Harness()
    h.tick()
    status, number = asyncio.run(h.loop.action_result(KEY, "dismiss"))
    assert status == "dismissed" and number is None
    assert h.state() is None
    assert len(h.gh.comments()) == 1 and len(h.gh.closes()) == 1
    assert "dismissed" in h.ctx.kinds()


def test_dismiss_with_no_issue_in_watch_mode_writes_nothing():
    h = Harness(mode="watch")
    h.tick()
    status, _ = asyncio.run(h.loop.action_result(KEY, "dismiss"))
    assert status == "dismissed"
    assert h.state() is None and not h.gh.calls


def test_muted_incident_counts_separately_and_drives_active_level():
    """F3: a muted incident must not keep the card at error/warn level, but it
    is still visible in the total."""
    h = Harness()
    h.tick()
    asyncio.run(h.loop.action_result(KEY, "mute"))
    h.tick()   # re-run so _stash_counts reflects the mute
    counts = h.ctx.deploy_monitor_counts
    assert counts["failing"] == 0 and counts["muted"] == 1 and counts["incidents"] == 1
    status = dm.DeployMonitor().status(h.ctx)
    assert status.level == "active" and "(1 muted)" in status.text


def test_incident_alert_is_info_level_not_warn():
    """F5: `warn` also routes through automation_warn, so emitting the incident
    at warn would double-push a wildcard notification rule — deploy_failed
    (via ``notify``) is the actual alert."""
    h = Harness()
    h.tick()
    incident_events = [e for e in h.ctx.events if e["kind"] == "incident"]
    assert incident_events and all(e["level"] == "info" for e in incident_events)


def test_regression_retried_after_a_failed_create_keeps_the_link():
    """m1: a failed regression create used to lose ``regression_of`` on retry —
    the row's ``issue_number`` was cleared to None but nothing remembered which
    issue it was regressing, so the retry filed as a fresh incident."""
    h = Harness()
    h.tick()
    h.gh.closed = {101: "completed"}
    h.now = T0 + timedelta(hours=1)
    h.tick()   # acknowledged at the closing sha
    h.deployments = {REPO: [_dep(sha="b" * 40), _dep()]}
    h.gh.closed = {}
    h.gh.fail_create = True
    h.now = T0 + timedelta(hours=2)
    h.tick()   # regression create attempt fails
    assert h.state()["issue_number"] is None
    assert h.state()["regression_of"] == 101
    h.gh.fail_create = False
    h.now = T0 + timedelta(hours=3)
    h.tick()   # retried — must still carry the regression link
    assert h.state()["issue_number"] == 102
    last_create = h.gh.creates()[-1]
    body_arg = next(a for a in last_create if a.startswith("body="))
    assert "Regression of" in body_arg and "#101" in body_arg
    assert "regressed" in h.ctx.kinds()


def test_sync_issue_states_learns_closure_on_a_manually_muted_row():
    """m4: a manual ``mute`` action (unlike a not_planned close) leaves
    ``issue_closed`` False. Sync must still learn the issue was closed, or
    ``_recover`` will later PATCH an issue a human already closed."""
    h = Harness()
    h.tick()
    asyncio.run(h.loop.action_result(KEY, "mute"))
    h.gh.closed = {101: "completed"}
    h.now = T0 + timedelta(hours=1)
    h.tick()
    assert h.state()["issue_closed"] is True
    h.deployments = {REPO: [_dep(state="SUCCESS")]}
    h.tick()
    assert h.state() is None and not h.gh.closes()
