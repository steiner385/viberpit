import pytest

from server.automations.action_providers import (
    PrDoctorActionProvider, BacklogRunnerActionProvider, CicdDoctorActionProvider,
)
from server.github.pr_status import PullRequestInfo


class _FakePrOrch:
    def __init__(self, pr):
        self._pr = pr
        self.fix_calls = []

    async def _pr_doctor_scope_pairs(self):
        return [(self._pr.repo, self._pr.head_ref)]

    def pr_for(self, repo, branch):
        return self._pr if (repo, branch) == (self._pr.repo, self._pr.head_ref) else None

    def _pr_population_pr(self, repo, branch):
        return None

    def _pr_doctor_owning_rec(self, repo, branch):
        return None

    async def _pr_doctor_trigger_async(self, repo, branch, info, kind, rec):
        # Async path (issue #668): record the hand-off and hand back a job id.
        self.fix_calls.append((repo, branch, kind))
        return "prd-test01"


def _pr(**kw):
    base = dict(number=11006, url="https://x/11006", state="open", checks="failing",
                title="sweep limiters", repo="cairnea/KinDash", head_ref="rl",
                merge_state_status="", is_draft=False, mergeable="UNKNOWN")
    base.update(kw)
    return PullRequestInfo(**base)


@pytest.mark.asyncio
async def test_pr_provider_actionable_maps_failing_ci_to_fix_ci_button():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr()))
    items = await prov.actionable()
    assert len(items) == 1
    it = items[0]
    assert it.id == "cairnea/KinDash#11006"
    assert it.title == "sweep limiters"
    assert [(a.kind, a.label, a.style) for a in it.actions] == [
        ("failing_ci", "Fix CI", "default")]


@pytest.mark.asyncio
async def test_pr_provider_merge_conflict_is_risky():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr(merge_state_status="DIRTY")))
    items = await prov.actionable()
    assert items[0].actions[0].kind == "merge_conflict"
    assert items[0].actions[0].style == "risky"


@pytest.mark.asyncio
async def test_pr_provider_trigger_queues_async_job():
    # issue #668: an actionable failing_ci PR hands off to the background job
    # pipeline and answers immediately with a queued result carrying the job id.
    orch = _FakePrOrch(_pr())
    prov = PrDoctorActionProvider(orch)
    res = await prov.trigger("cairnea/KinDash#11006", "failing_ci")
    assert (res.status, res.level) == ("queued", "ok")
    assert (res.job_id, res.phase) == ("prd-test01", "queued")
    assert res.to_dict()["job_id"] == "prd-test01"
    assert orch.fix_calls == [("cairnea/KinDash", "rl", "failing_ci")]


@pytest.mark.asyncio
async def test_pr_provider_trigger_out_of_date_stays_synchronous():
    # Mechanical update-branch is fast — it must NOT be routed through the async
    # job path (no job id, real terminal status).
    orch = _FakePrOrch(_pr())
    orch._pr_doctor_mechanical = _mechanical_ok  # type: ignore[attr-defined]
    prov = PrDoctorActionProvider(orch)
    res = await prov.trigger("cairnea/KinDash#11006", "out_of_date")
    assert (res.status, res.level) == ("updated", "ok")
    assert res.job_id is None
    assert "job_id" not in res.to_dict()
    assert orch.fix_calls == []  # async path not taken


async def _mechanical_ok(repo, info, kind):
    return True


@pytest.mark.asyncio
async def test_pr_provider_trigger_unknown_item_is_no_pr():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr()))
    res = await prov.trigger("cairnea/KinDash#999", "failing_ci")
    assert (res.status, res.level) == ("no_pr", "warn")


class _FakeBacklogOrch:
    def __init__(self, status="spawned"):
        self._status = status
        self.implement_calls = []
        self.dispatch_calls: list[list[str]] = []

    async def backlog_runner_implement_core(self, primary):
        self.implement_calls.append(primary)
        return self._status

    async def backlog_runner_dispatch(self, refs):
        self.dispatch_calls.append(refs)
        return {"status": self._status, "session_id": "s-1", "session_name": "backlog"}

    async def backlog_candidates(self):
        return [{"ref": "cairnea/KinDash#42", "repo": "cairnea/KinDash",
                 "title": "add rate limiter",
                 "url": "https://github.com/cairnea/KinDash/issues/42"}]


@pytest.mark.asyncio
async def test_backlog_provider_actionable_and_trigger():
    orch = _FakeBacklogOrch(status="no_account")
    prov = BacklogRunnerActionProvider(orch)
    items = await prov.actionable()
    assert items[0].id == "cairnea/KinDash#42"
    assert items[0].url == "https://github.com/cairnea/KinDash/issues/42"  # #467
    assert items[0].actions[0].kind == "implement"
    res = await prov.trigger("cairnea/KinDash#42", "implement")
    assert (res.status, res.level) == ("no_account", "warn")
    assert "usage ceiling" in res.message
    assert orch.implement_calls == ["cairnea/KinDash#42"]


class _FakeCicdOrch:
    def __init__(self, status):
        self._status = status
        self.file_calls = []

    async def cicd_actionable_findings(self):
        return [{"id": "f-1", "repo": "o/r", "title": "flaky test: t::a",
                 "kind": "flaky_test"}]

    async def cicd_file_issue_now(self, finding_id):
        self.file_calls.append(finding_id)
        return self._status


@pytest.mark.asyncio
async def test_cicd_provider_actionable_and_file():  # #465
    orch = _FakeCicdOrch(status="filed")
    prov = CicdDoctorActionProvider(orch)
    items = await prov.actionable()
    assert items[0].id == "f-1"
    assert items[0].subtitle == "o/r"
    assert items[0].actions[0].kind == "file_issue"
    res = await prov.trigger("f-1", "file_issue")
    assert (res.status, res.level) == ("filed", "ok")
    assert orch.file_calls == ["f-1"]


@pytest.mark.asyncio
async def test_cicd_provider_already_filed_warns():
    orch = _FakeCicdOrch(status="exists")
    res = await CicdDoctorActionProvider(orch).trigger("f-1", "file_issue")
    assert (res.status, res.level) == ("exists", "warn")
    assert "already filed" in res.message
