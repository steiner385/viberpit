# server/tests/test_build_check_groups.py
import math
from datetime import datetime, timezone

from server.pipeline.checkgroups import build_check_groups, RUNNER_RATE_USD_PER_MIN


class _FakeExpected:
    def __init__(self, p50): self.p50 = p50


class _FakeStore:
    def __init__(self, p50s): self._p50s = p50s
    def expected(self, repo, name, event):
        v = self._p50s.get(name)
        return _FakeExpected(v) if v is not None else None


NOW = datetime(2026, 6, 28, 12, 10, 0, tzinfo=timezone.utc)


def _head(name, started, completed, conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "started_at": started, "completed_at": completed,
            "isRequired": True, "workflowName": "CI", "event": "pull_request"}


def test_completed_check_duration_and_cost():
    head = [_head("lint", "2026-06-28T12:00:00Z", "2026-06-28T12:05:00Z")]
    groups = build_check_groups(head, {"checks": [], "eta_seconds": None},
                                _FakeStore({}), "octo/repo", NOW)
    assert len(groups) == 1  # no merge group when empty
    g = groups[0]
    assert g.kind == "head"
    assert g.checks[0].duration_seconds == 300
    assert g.checks[0].conclusion == "success"
    assert g.total_seconds == 300
    assert g.cost_usd == round(math.ceil(300 / 60) * RUNNER_RATE_USD_PER_MIN, 4)


def test_running_check_overdue_vs_p50():
    # started 10m ago, still running, p50 expected 120s -> overdue
    head = [_head("slow", "2026-06-28T12:00:00Z", None, conclusion=None, status="IN_PROGRESS")]
    groups = build_check_groups(head, {"checks": [], "eta_seconds": None},
                                _FakeStore({"slow": 120.0}), "octo/repo", NOW)
    row = groups[0].checks[0]
    assert row.duration_seconds == 600  # elapsed now - started
    assert row.overdue is True
    assert row.conclusion == "pending"  # null conclusion while running


def test_running_check_no_history_not_overdue():
    head = [_head("new", "2026-06-28T12:09:00Z", None, conclusion=None, status="IN_PROGRESS")]
    groups = build_check_groups(head, {"checks": [], "eta_seconds": None},
                                _FakeStore({}), "octo/repo", NOW)
    assert groups[0].checks[0].overdue is False


def test_merge_group_appended_with_eta():
    head = [_head("lint", "2026-06-28T12:00:00Z", "2026-06-28T12:01:00Z")]
    merge = {"checks": [{"name": "e2e", "status": "COMPLETED", "conclusion": "SUCCESS",
                         "started_at": "2026-06-28T12:00:00Z",
                         "completed_at": "2026-06-28T12:02:00Z",
                         "isRequired": False, "workflowName": "E2E", "event": "merge_group"}],
             "eta_seconds": 180}
    groups = build_check_groups(head, merge, _FakeStore({}), "octo/repo", NOW)
    assert [g.kind for g in groups] == ["head", "merge"]
    assert groups[1].eta_seconds == 180
    assert groups[1].checks[0].name == "e2e"


class _EventAwareFakeStore:
    """Fake store that keys expected duration on (name, event) so we can assert
    that _row passes the check's own event, not a hardcoded constant."""
    def __init__(self, data: dict):
        # data: {(name, event): p50}
        self._data = data

    def expected(self, repo, name, event):
        v = self._data.get((name, event))
        return _FakeExpected(v) if v is not None else None


def test_merge_group_check_uses_own_event_for_history():
    """A running merge_group check whose p50 history exists ONLY under event=merge_group
    must be flagged overdue=True.  With the old hardcoded _EVENT="pull_request" lookup
    store.expected() returns None → overdue stays False (the regression this test covers)."""
    # p50=120s exists only for event="merge_group"; pull_request has no entry
    store = _EventAwareFakeStore({("mg-lint", "merge_group"): 120.0})
    # started 10 min ago, still running → elapsed=600s > p50=120s → overdue
    merge_check = {
        "name": "mg-lint",
        "status": "IN_PROGRESS",
        "conclusion": None,
        "started_at": "2026-06-28T12:00:00Z",
        "completed_at": None,
        "isRequired": True,
        "workflowName": "CI / Merge",
        "event": "merge_group",
    }
    groups = build_check_groups([], {"checks": [merge_check], "eta_seconds": None},
                                store, "octo/repo", NOW)
    assert len(groups) == 2  # head (empty) + merge
    assert groups[1].kind == "merge"
    row = groups[1].checks[0]
    assert row.duration_seconds == 600
    assert row.overdue is True, (
        "merge_group check must be flagged overdue when its own-event p50 is exceeded; "
        "a pull_request-keyed lookup would return None and leave overdue=False"
    )
