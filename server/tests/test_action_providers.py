import pytest

from server.automations.action_providers import (
    PrDoctorActionProvider, BacklogRunnerActionProvider,
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

    async def _pr_doctor_session_fix(self, repo, branch, info, kind, rec):
        self.fix_calls.append((repo, branch, kind))
        return "ceiling", None


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
async def test_pr_provider_trigger_maps_ceiling_to_warn():
    orch = _FakePrOrch(_pr())
    prov = PrDoctorActionProvider(orch)
    res = await prov.trigger("cairnea/KinDash#11006", "failing_ci")
    assert (res.status, res.level) == ("ceiling", "warn")
    assert "usage ceiling" in res.message
    assert orch.fix_calls == [("cairnea/KinDash", "rl", "failing_ci")]


@pytest.mark.asyncio
async def test_pr_provider_trigger_unknown_item_is_no_pr():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr()))
    res = await prov.trigger("cairnea/KinDash#999", "failing_ci")
    assert (res.status, res.level) == ("no_pr", "warn")


class _FakeBacklogOrch:
    def __init__(self, status="spawned"):
        self._status = status
        self.implement_calls = []

    async def backlog_runner_implement_core(self, primary):
        self.implement_calls.append(primary)
        return self._status

    async def backlog_candidates(self):
        return [{"ref": "cairnea/KinDash#42", "repo": "cairnea/KinDash",
                 "title": "add rate limiter"}]


@pytest.mark.asyncio
async def test_backlog_provider_actionable_and_trigger():
    orch = _FakeBacklogOrch(status="no_account")
    prov = BacklogRunnerActionProvider(orch)
    items = await prov.actionable()
    assert items[0].id == "cairnea/KinDash#42"
    assert items[0].actions[0].kind == "implement"
    res = await prov.trigger("cairnea/KinDash#42", "implement")
    assert (res.status, res.level) == ("no_account", "warn")
    assert "usage ceiling" in res.message
    assert orch.implement_calls == ["cairnea/KinDash#42"]
