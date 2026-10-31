"""SystemicRedLoop: the incident state machine over injected seams (spec §3c-§6)."""
from __future__ import annotations

import asyncio
import calendar
import json
import re
import time

from server.automations.cicd.systemic import JobObs, RunObs
from server.automations.cicd.systemic_loop import SystemicRedLoop

REPO = "cairnea/KinDash"
MIN = 60.0
HOUR = 60 * MIN
T0 = 1_793_408_400.0  # 2026-10-31T01:00:00Z
WF = 100
MAIN_SHA = "a1a1a1a1"
AUDIT = "static-checks / security: audit"
LABEL = "ci:systemic-red"
MARKER_RE = re.compile(r"<!-- viberpit:systemic-brake (\{.*?\}) -->")

_ids = iter(range(1, 1_000_000))


def _job(name: str, conclusion: str, done: float) -> JobObs:
    return JobObs(name=name, conclusion=conclusion, status="completed", completed_ts=done,
                  job_id=next(_ids))


def _cand(pr: int, created: float, job: str = AUDIT, conclusion: str = "failure", *,
          base: str = MAIN_SHA) -> RunObs:
    rid = next(_ids)
    return RunObs(run_id=rid, workflow_id=WF, event="merge_group",
                  head_branch=f"gh-readonly-queue/main/pr-{pr}-{base}",
                  head_sha=f"c0ffee{rid:06x}", created_ts=created, status="completed",
                  conclusion=conclusion, jobs=(_job(job, conclusion, created + 5 * MIN),
                                               _job("ci", conclusion, created + 6 * MIN)))


def _main(created: float, job: str = AUDIT, conclusion: str = "success") -> RunObs:
    return RunObs(run_id=next(_ids), workflow_id=WF, event="push", head_branch="main",
                  head_sha=MAIN_SHA, created_ts=created, status="completed",
                  conclusion=conclusion, jobs=(_job(job, conclusion, created + 5 * MIN),))


def _red_audit(start: float = T0) -> list[RunObs]:
    return [_cand(pr, start + i * MIN) for i, pr in enumerate((20234, 20304, 20305))]


class FakeCtx:
    def __init__(self):
        self.events: list[dict] = []
        self.states: dict[str, dict] = {}

    async def emit(self, level, summary, *, kind=None, target=None, session_id=None, detail=None):
        self.events.append({"level": level, "summary": summary, "kind": kind, "target": target})

    async def get_target_state(self, target):
        return dict(self.states.get(target, {}))

    async def set_target_state(self, target, state):
        self.states[target] = json.loads(json.dumps(state))

    async def delete_target_state(self, target):
        self.states.pop(target, None)

    async def list_target_states(self):
        return {k: dict(v) for k, v in self.states.items()}

    def incidents(self) -> dict[str, dict]:
        return {k: v for k, v in self.states.items() if k.startswith(f"systemic:{REPO}:")}


class Seams:
    """In-memory GitHub, Actions, push, session and AWS seams."""

    def __init__(self, mode: str = "correct", **config):
        self.mode = mode
        self.config = dict(config)
        self.now = T0 + 20 * MIN
        self.merge_group: list[RunObs] = []
        self.push: list[RunObs] = []
        self.fail_fetch = False
        self.fail_edit = False
        self.fail_label = False
        self.issues: dict[int, dict] = {}
        self.next_issue = 500
        self.calls: list[tuple] = []
        self.notes: list[tuple] = []
        self.spawn_result: str | None = "sess-1"
        self.spawn_created = True  # False = joined an existing session (dedupe hit)
        self.spawns: list[tuple] = []
        self.hourly: tuple[int, list[int], float, int] = (0, [1] * 24, 0.0, 0)
        self.alarms: list[dict] | None = []
        self.alarm_calls = 0

    def loop(self, ctx: FakeCtx) -> SystemicRedLoop:
        return SystemicRedLoop(
            ctx, read_config=self.read_config, list_repos=self.list_repos,
            fetch_runs=self.fetch_runs, fetch_log=self.fetch_log, file_issue=self.file_issue,
            comment_issue=self.comment_issue, edit_issue_body=self.edit_issue_body,
            add_label=self.add_label, remove_label=self.remove_label,
            close_issue=self.close_issue, issue_snapshot=self.issue_snapshot,
            notify=self.notify, spawn_fix=self.spawn_fix,
            failed_hourly_counts=self.failed_hourly_counts,
            alarms_in_alarm=self.alarms_in_alarm, clock=lambda: self.now)

    async def read_config(self):
        return self.mode, dict(self.config)

    async def list_repos(self, config):
        return [REPO]

    async def fetch_runs(self, repo, event, since, branch):
        if self.fail_fetch:
            raise RuntimeError("gh down")
        pool = self.merge_group if event == "merge_group" else self.push
        return [r for r in pool if since <= r.created_ts <= self.now]

    async def fetch_log(self, repo, job_id):
        return "2026-10-31T01:01:00Z ##[error]Found 4 vulnerabilities (2 high)\n"

    async def file_issue(self, repo, title, body, labels):
        self.next_issue += 1
        self.issues[self.next_issue] = {"state": "open", "body": body, "labels": list(labels),
                                        "title": title}
        self.calls.append(("file", self.next_issue, tuple(labels)))
        return self.next_issue

    async def comment_issue(self, repo, number, body):
        self.calls.append(("comment", number, body))
        return True

    async def edit_issue_body(self, repo, number, body):
        self.calls.append(("edit", number))
        if self.fail_edit:
            return False
        self.issues[number]["body"] = body
        return True

    async def add_label(self, repo, number, label):
        self.calls.append(("add_label", number, label))
        if self.fail_label:
            return False
        if label not in self.issues[number]["labels"]:
            self.issues[number]["labels"].append(label)
        return True

    async def remove_label(self, repo, number, label):
        self.calls.append(("remove_label", number, label))
        if label not in self.issues[number]["labels"]:
            return False  # like ci_issues.remove_label on GitHub's 404
        self.issues[number]["labels"].remove(label)
        return True

    async def close_issue(self, repo, number):
        self.calls.append(("close", number))
        self.issues[number]["state"] = "closed"
        return True

    async def issue_snapshot(self, repo, number):
        i = self.issues.get(number)
        return None if i is None else {"state": i["state"], "body": i["body"],
                                       "labels": list(i["labels"])}

    async def notify(self, event_class, title, body, *, repo, url):
        self.notes.append((event_class, title, body))

    async def spawn_fix(self, repo, origin_ref, prompt):
        self.spawns.append((repo, origin_ref, prompt))
        return (self.spawn_result, self.spawn_created) if self.spawn_result else None

    async def failed_hourly_counts(self, repo):
        return self.hourly

    async def alarms_in_alarm(self, config):
        self.alarm_calls += 1
        return self.alarms

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]

    def marker(self, number: int) -> dict | None:
        m = MARKER_RE.search(self.issues[number]["body"])
        return json.loads(m.group(1)) if m else None


def _tick(loop: SystemicRedLoop) -> None:
    asyncio.run(loop.tick())


def _only_incident(ctx: FakeCtx) -> dict:
    inc = ctx.incidents()
    assert len(inc) == 1, inc
    return next(iter(inc.values()))


def _iso_ts(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def test_propose_mode_alerts_and_files_once_but_never_brakes_or_dispatches():
    s, ctx = Seams("propose"), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    s.now += 120
    _tick(loop)
    assert s.kinds().count("file") == 1
    assert [c for c in s.calls if c[0] == "file"][0][2] == ("ci-doctor", "ci-systemic")
    assert [n[0] for n in s.notes] == ["ci_systemic_red"]
    assert "add_label" not in s.kinds() and "edit" not in s.kinds()
    assert s.spawns == []
    st = _only_incident(ctx)
    assert st["class"] == "external_feed" and st["brake_state"] == "none"
    assert st["alerted_open"] is True and st["prs"] == [20234, 20304, 20305]
    assert ctx.cicd_systemic_counts == {"incidents": 1, "braked": 0, "thrash": False, "alarms": 0}


def test_correct_mode_external_feed_sets_verified_brake_and_dispatches_once():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    st = _only_incident(ctx)
    n = st["issue_number"]
    assert st["brake_state"] == "set"
    assert LABEL in s.issues[n]["labels"]
    marker = s.marker(n)
    assert marker is not None and marker["job"] == "security: audit"
    assert marker["class"] == "external_feed" and marker["by"] == "viberpit-cicd-doctor"
    assert _iso_ts(marker["expires_at"]) == s.now + 240 * MIN
    assert len(s.spawns) == 1
    repo, origin, prompt = s.spawns[0]
    assert repo == REPO and origin == f"{REPO}#systemic:{st['fingerprint']}"
    assert "never mute" in prompt.lower() and "merge queue" in prompt
    assert st["session_id"] == "sess-1"
    assert len(s.notes) == 1 and "braked" in s.notes[0][1]
    s.now += 120
    _tick(loop)
    assert len(s.spawns) == 1 and len(s.notes) == 1 and s.kinds().count("file") == 1
    assert ctx.cicd_systemic_counts["braked"] == 1


def _poison_queue() -> list[RunObs]:
    job = "test: unit (3/4)"
    out, base = [], MAIN_SHA
    for i, pr in enumerate((9, 10, 11, 12)):
        c = _cand(pr, T0 + i * MIN, job=job, base=base)
        base = c.head_sha
        out.append(c)
    # A candidate whose stack lacks PR 9 ran the job green before the chain.
    out.append(_cand(13, T0 - 10 * MIN, job=job, conclusion="success"))
    return out


def test_poisoner_never_brakes_and_never_dispatches():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _poison_queue()
    s.push = [_main(T0 - HOUR, job="test: unit (3/4)")]
    loop = s.loop(ctx)
    _tick(loop)
    st = _only_incident(ctx)
    assert st["class"] == "poisoner" and st["poisoner_pr"] == 9
    assert "add_label" not in s.kinds() and "edit" not in s.kinds()
    assert s.spawns == []
    body = s.issues[st["issue_number"]]["body"]
    assert "dequeue PR #9 under the queue lock" in body
    assert f"gh pr view 9 -R {REPO} --json id -q .id" in body and "```sh" in body
    assert len(s.notes) == 1


def test_brake_ttl_renewed_each_tick_and_lifted_at_ceiling_without_rebrake():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    n = _only_incident(ctx)["issue_number"]
    set_ts = s.now
    first = _iso_ts(s.marker(n)["expires_at"])  # type: ignore[index]
    s.now += 120
    _tick(loop)
    assert _iso_ts(s.marker(n)["expires_at"]) == first + 120  # type: ignore[index]
    # Near the ceiling the expiry is capped at set_at + max.
    s.now = set_ts + 500 * MIN
    _tick(loop)
    assert _iso_ts(s.marker(n)["expires_at"]) == set_ts + 600 * MIN  # type: ignore[index]
    s.now = set_ts + 600 * MIN
    _tick(loop)
    st = _only_incident(ctx)
    assert st["brake_state"] == "ceiling"
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    ceiling_notes = [x for x in s.notes if "brake lifted after 10 h" in x[1]]
    assert len(ceiling_notes) == 1
    calls_before = len(s.calls)
    s.now += 120
    _tick(loop)
    assert not [c for c in s.calls[calls_before:] if c[0] in ("add_label", "edit")]
    assert len([x for x in s.notes if "brake lifted after 10 h" in x[1]]) == 1


def test_clear_lifts_brake_comments_closes_and_notifies_once():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    n = _only_incident(ctx)["issue_number"]
    s.now += 120
    s.merge_group.append(_cand(20306, s.now - 1, conclusion="success"))
    s.now += 6 * MIN  # the green job completed
    _tick(loop)
    assert ctx.incidents() == {}
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    assert s.issues[n]["state"] == "closed"
    assert [c for c in s.calls if c[0] == "comment"]
    cleared = [x for x in s.notes if "cleared" in x[1]]
    assert len(cleared) == 1
    s.now += 120
    _tick(loop)
    assert len([x for x in s.notes if "cleared" in x[1]]) == 1


def test_restart_resumes_incident_without_duplicate_side_effects():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    _tick(s.loop(ctx))
    n = _only_incident(ctx)["issue_number"]
    s.now += 120
    _tick(s.loop(ctx))  # a fresh instance over the same persisted state
    assert s.kinds().count("file") == 1
    assert len(s.spawns) == 1 and len(s.notes) == 1
    assert _only_incident(ctx)["brake_state"] == "set"
    assert _iso_ts(s.marker(n)["expires_at"]) == s.now + 240 * MIN  # type: ignore[index]


def test_failed_brake_write_is_not_reported_as_set():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    s.fail_edit = True
    loop = s.loop(ctx)
    _tick(loop)
    st = _only_incident(ctx)
    assert st["brake_state"] == "failed"
    assert any("brake NOT set" in e["summary"] for e in ctx.events)
    assert "braked" not in s.notes[0][1] and "brake NOT set" in s.notes[0][1]
    assert ctx.cicd_systemic_counts["braked"] == 0
    s.fail_edit = False
    s.now += 120
    _tick(loop)
    assert _only_incident(ctx)["brake_state"] == "set"


def test_owner_closed_issue_is_not_reopened_or_rebraked():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    n = _only_incident(ctx)["issue_number"]
    s.issues[n]["state"] = "closed"
    mark = len(s.calls)
    for _ in range(3):
        s.now += 120
        _tick(loop)
    st = _only_incident(ctx)
    assert st["owner_closed"] is True and st["brake_state"] == "lifted"
    assert s.calls[mark:] == []
    assert s.issues[n]["state"] == "closed"
    # It still clears normally: state dropped, no comment on the closed issue.
    s.merge_group.append(_cand(20306, s.now - 1, conclusion="success"))
    s.now += 6 * MIN
    _tick(loop)
    assert ctx.incidents() == {}
    assert s.calls[mark:] == []
    assert len([x for x in s.notes if "cleared" in x[1]]) == 1  # the open push had gone out


def _green_after(s: Seams) -> None:
    s.now += 120
    s.merge_group.append(_cand(20306, s.now - 1, conclusion="success"))
    s.now += 6 * MIN


def test_clear_after_failed_brake_closes_incident():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    s.fail_label = True  # the label never lands; the marker does
    loop = s.loop(ctx)
    _tick(loop)
    n = _only_incident(ctx)["issue_number"]
    assert _only_incident(ctx)["brake_state"] == "failed" and s.marker(n) is not None
    _green_after(s)
    _tick(loop)
    assert ctx.incidents() == {}
    assert s.issues[n]["state"] == "closed" and s.marker(n) is None
    assert ("remove_label", n, LABEL) not in s.calls


def test_clear_after_owner_removed_label():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    n = _only_incident(ctx)["issue_number"]
    s.issues[n]["labels"].remove(LABEL)  # a human took the label off by hand
    _green_after(s)
    _tick(loop)
    assert ctx.incidents() == {}
    assert s.issues[n]["state"] == "closed" and s.marker(n) is None
    comment = [c for c in s.calls if c[0] == "comment"][0][2]
    assert "Brake lifted" in comment


def test_recovery_comment_omits_brake_when_none_was_set():
    s, ctx = Seams("propose"), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    _green_after(s)
    _tick(loop)
    comment = [c for c in s.calls if c[0] == "comment"][0][2]
    assert "Brake" not in comment and "brake" not in comment


def test_reclassified_to_poisoner_lifts_brake_and_never_rebrakes():
    job = "test: unit (3/4)"
    s, ctx = Seams(systemic_external_feed_jobs=r"^test: unit"), FakeCtx()
    s.merge_group = _poison_queue()
    s.push = [_main(T0 - HOUR, job=job)]
    loop = s.loop(ctx)
    _tick(loop)
    st = _only_incident(ctx)
    n = st["issue_number"]
    assert st["class"] == "external_feed" and st["brake_state"] == "set"
    assert len(s.spawns) == 1
    s.config["systemic_external_feed_jobs"] = "^security: audit$"
    s.now += 120
    _tick(loop)
    st = _only_incident(ctx)
    assert st["class"] == "poisoner" and st["brake_state"] == "lifted"
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    s.config["systemic_external_feed_jobs"] = r"^test: unit"  # flips back
    mark = len(s.calls)
    s.now += 120
    _tick(loop)
    st = _only_incident(ctx)
    assert st["class"] == "external_feed" and st["brake_state"] == "lifted"
    assert not [c for c in s.calls[mark:] if c[0] in ("add_label", "edit")]
    assert len(s.spawns) == 1


def _braked(mode_after: str) -> tuple[Seams, FakeCtx, SystemicRedLoop, int]:
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    n = _only_incident(ctx)["issue_number"]
    assert _only_incident(ctx)["brake_state"] == "set"
    s.mode = mode_after
    s.now += 120
    return s, ctx, loop, n


def test_switching_to_propose_lifts_brake():
    s, ctx, loop, n = _braked("propose")
    _tick(loop)
    assert _only_incident(ctx)["brake_state"] == "lifted"
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    s.mode = "correct"
    s.now += 120
    _tick(loop)
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None


def test_brake_knob_off_lifts_brake():
    s, ctx, loop, n = _braked("correct")
    s.config["systemic_brake"] = False
    _tick(loop)
    assert _only_incident(ctx)["brake_state"] == "lifted" and s.marker(n) is None


def test_off_mode_lifts_live_brake():
    s, ctx, loop, n = _braked("off")
    s.fail_fetch = True  # the lift-only pass reads nothing from Actions
    notes = len(s.notes)
    _tick(loop)
    assert _only_incident(ctx)["brake_state"] == "lifted"
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    assert s.issues[n]["state"] == "open" and len(s.notes) == notes
    assert ctx.cicd_systemic_counts == {"incidents": 0, "braked": 0, "thrash": False, "alarms": 0}


def test_daily_dispatch_cap():
    s, ctx = Seams(systemic_max_fix_sessions_per_day=1), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    s.spawn_result = None  # not spawned: retry next tick, counter untouched
    loop = s.loop(ctx)
    _tick(loop)
    day = time.strftime("%Y-%m-%d", time.gmtime(s.now))
    assert ctx.states.get(f"systemic-dispatch:{REPO}:{day}", {}).get("count", 0) == 0
    assert _only_incident(ctx)["session_id"] is None
    s.spawn_result = "sess-9"
    s.now += 120
    _tick(loop)
    assert ctx.states[f"systemic-dispatch:{REPO}:{day}"]["count"] == 1
    # A second incident the same day hits the cap of 1.
    s.push.append(_main(s.now - 30 * MIN, job="docs: conformance", conclusion="failure"))
    s.merge_group.append(_cand(20400, s.now - 20 * MIN, job="docs: conformance"))
    _tick(loop)
    assert len(ctx.incidents()) == 2
    assert len(s.spawns) == 2  # the None attempt + one real spawn
    assert ctx.states[f"systemic-dispatch:{REPO}:{day}"]["count"] == 1


def test_fetch_failure_changes_nothing():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    before = json.loads(json.dumps(ctx.states))
    calls = len(s.calls)
    s.fail_fetch = True
    s.merge_group.append(_cand(20306, s.now, conclusion="success"))
    s.now += 10 * MIN
    _tick(loop)  # must not raise
    assert ctx.states == before
    assert s.calls[calls:] == [] and len(s.notes) == 1


def test_thrash_alerts_once_until_it_resets():
    s, ctx = Seams(), FakeCtx()
    loop = s.loop(ctx)
    s.hourly = (12, [1] * 48, 0.9, 13)
    _tick(loop)
    s.now += 120
    _tick(loop)
    thrash = [x for x in s.notes if x[0] == "ci_systemic_red"]
    assert len(thrash) == 1 and "12" in thrash[0][2]
    assert any(e["level"] == "warn" for e in ctx.events)
    assert ctx.cicd_systemic_counts["thrash"] is True
    s.hourly = (1, [1] * 48, 0.1, 10)
    s.now += 120
    _tick(loop)
    assert f"systemic-thrash:{REPO}" not in ctx.states
    s.hourly = (12, [1] * 48, 0.9, 13)
    s.now += 120
    _tick(loop)
    assert len([x for x in s.notes if x[0] == "ci_systemic_red"]) == 2


def test_new_aws_alarm_notifies_once():
    s, ctx = Seams(aws_alarm_prefix="cairnea-ci-"), FakeCtx()
    loop = s.loop(ctx)
    s.alarms = [{"name": "cairnea-ci-vcpu-24h", "reason": "over", "updated": ""}]
    _tick(loop)
    assert [x[0] for x in s.notes] == ["ci_spend_alarm"]
    s.now += 120
    _tick(loop)
    assert s.alarm_calls == 1  # polled at most every 300 s
    s.now += 300
    _tick(loop)
    assert s.alarm_calls == 2 and len(s.notes) == 1
    assert ctx.cicd_systemic_counts["alarms"] == 1
    s.alarms = None  # a failed read forgets nothing
    s.now += 300
    _tick(loop)
    assert "systemic-alarm:cairnea-ci-vcpu-24h" in ctx.states
    s.alarms = []
    s.now += 300
    _tick(loop)
    assert "systemic-alarm:cairnea-ci-vcpu-24h" not in ctx.states
    s.alarms = [{"name": "cairnea-ci-vcpu-24h", "reason": "over", "updated": ""}]
    s.now += 300
    _tick(loop)
    assert len(s.notes) == 2


def test_off_mode_does_nothing():
    s, ctx = Seams("off", aws_alarm_prefix="cairnea-ci-"), FakeCtx()
    s.merge_group = _red_audit()
    s.hourly = (12, [1] * 48, 0.9, 13)
    s.alarms = [{"name": "cairnea-ci-x", "reason": "", "updated": ""}]
    _tick(s.loop(ctx))
    assert ctx.states == {} and s.calls == [] and s.notes == [] and s.alarm_calls == 0


def test_brake_marker_expiry_never_exceeds_twelve_hours():
    # Guard against config that would breach the KinDash consumer's 12 h cap.
    s, ctx = Seams(systemic_brake_ttl_min=1000, systemic_brake_max_min=2000), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    _tick(s.loop(ctx))
    n = _only_incident(ctx)["issue_number"]
    assert _iso_ts(s.marker(n)["expires_at"]) == s.now + 12 * HOUR  # type: ignore[index]


def test_clear_brake_action_lifts_and_never_rebrakes():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    key, st = next(iter(ctx.incidents().items()))
    n = st["issue_number"]
    assert st["brake_state"] == "set"
    items = asyncio.run(loop.actionable_items())
    assert [i.id for i in items] == [key]
    assert {a.kind for a in items[0].actions} == {"clear_brake", "dismiss"}
    assert items[0].url == f"https://github.com/{REPO}/issues/{n}"

    assert asyncio.run(loop.action_result(key, "clear_brake")) == "brake_cleared"
    st = _only_incident(ctx)
    assert st["brake_state"] == "lifted"
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    assert s.issues[n]["state"] == "open"  # the issue closes only on clear
    items = asyncio.run(loop.actionable_items())
    assert {a.kind for a in items[0].actions} == {"dismiss"}
    mark = len(s.calls)
    for _ in range(3):  # still red: renewed every tick, but never re-braked
        s.now += 120
        _tick(loop)
    assert not [c for c in s.calls[mark:] if c[0] in ("add_label", "edit")]
    assert _only_incident(ctx)["brake_state"] == "lifted"
    assert asyncio.run(loop.action_result("systemic:o/r:nope", "clear_brake")) == "not_found"
    assert asyncio.run(loop.action_result(key, "mute")) == "failed"


def test_clear_brake_action_failed_lift_reports_failed():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    key, st = next(iter(ctx.incidents().items()))
    s.issues.pop(st["issue_number"])  # snapshot read fails -> cannot verify a lift
    assert asyncio.run(loop.action_result(key, "clear_brake")) == "failed"
    assert _only_incident(ctx)["brake_state"] == "set"


def test_dismiss_action_closes_and_forgets():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    key, st = next(iter(ctx.incidents().items()))
    n = st["issue_number"]
    assert asyncio.run(loop.action_result(key, "dismiss")) == "dismissed"
    st = _only_incident(ctx)
    assert st["dismissed"] is True and st["brake_state"] == "lifted"
    assert LABEL not in s.issues[n]["labels"] and s.marker(n) is None
    assert s.issues[n]["state"] == "closed"
    comments = [c for c in s.calls if c[0] == "comment" and c[1] == n]
    assert len(comments) == 1 and "dismissed" in comments[0][2].lower()
    assert any(e["kind"] == "systemic_dismissed" for e in ctx.events)
    assert asyncio.run(loop.actionable_items()) == []  # off the card
    mark = len(s.calls)
    assert asyncio.run(loop.action_result(key, "dismiss")) == "dismissed"  # idempotent
    assert asyncio.run(loop.action_result(key, "clear_brake")) == "not_found"
    assert s.calls[mark:] == []
    assert asyncio.run(loop.action_result("systemic:o/r:nope", "dismiss")) == "not_found"


def test_dismissed_incident_does_not_reopen_while_still_red():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    loop = s.loop(ctx)
    _tick(loop)
    key = next(iter(ctx.incidents()))
    assert asyncio.run(loop.action_result(key, "dismiss")) == "dismissed"
    s.now += 120
    _tick(loop)
    assert ctx.cicd_systemic_counts["incidents"] == 0  # off the card, not counted
    mark, notes, spawns = len(s.calls), len(s.notes), len(s.spawns)
    for i in range(4):  # still red, with a fresh failing candidate each tick
        s.now += 120
        s.merge_group.append(_cand(20400 + i, s.now - 30))
        _tick(loop)
    assert s.calls[mark:] == []  # no new issue, label, marker edit or comment
    assert len(s.notes) == notes and len(s.spawns) == spawns
    assert list(ctx.incidents()) == [key] and ctx.incidents()[key]["dismissed"] is True
    assert ctx.cicd_systemic_counts["braked"] == 0
    # The job goes green: the incident clears silently (no push, no comment).
    _green_after(s)
    _tick(loop)
    assert ctx.incidents() == {}
    assert s.calls[mark:] == [] and len(s.notes) == notes


def test_joined_fix_session_does_not_count_against_the_daily_cap():
    s, ctx = Seams(), FakeCtx()
    s.merge_group = _red_audit()
    s.push = [_main(T0 - HOUR)]
    s.spawn_created = False  # a live session for this incident already existed
    loop = s.loop(ctx)
    _tick(loop)
    day = time.strftime("%Y-%m-%d", time.gmtime(s.now))
    assert _only_incident(ctx)["session_id"] == "sess-1"
    assert ctx.states.get(f"systemic-dispatch:{REPO}:{day}", {}).get("count", 0) == 0
    assert not any(e["kind"] == "systemic_dispatch" for e in ctx.events)
    s.now += 120
    _tick(loop)
    assert len(s.spawns) == 1  # the joined id is recorded; no re-dispatch
