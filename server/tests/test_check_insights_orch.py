from datetime import datetime, timezone
from server.sessions.orchestrator import SessionOrchestrator
from server.pipeline.history import CheckDurationStat, CheckFlakeStat


class _FakeStore:
    def __init__(self):
        self.calls = []

    def slowest_checks(self, repo, since_iso, limit=10, event=None):
        self.calls.append(("slow", repo, since_iso))
        return [CheckDurationStat("c", "pr", 100.0, 200.0, 2.0, 5)]

    def flakiest_checks(self, repo, since_iso, limit=10, event=None):
        self.calls.append(("flaky", repo, since_iso))
        return [CheckFlakeStat("c", "pr", 50.0, 1, 2, 3)]

    def duration_regressions(self, repo):
        return []

    def runner_wait_health(self, repo, since, prev_since):
        return []


def _orch_with_store(store):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)  # skip heavy __init__

    class _Pop:
        def history_store(self_inner):
            return store

    orch._pr_population = _Pop()
    return orch


def test_check_insights_maps_window_to_cutoff_and_serializes():
    store = _FakeStore()
    orch = _orch_with_store(store)
    now = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)
    out = orch.check_insights("o/n", "7d", now=now)
    assert out["repo"] == "o/n" and out["window"] == "7d"
    assert out["slowest"][0]["p50"] == 100.0
    assert out["flakiest"][0]["flake_pct"] == 50.0
    # 7d before 2026-07-08 → 2026-07-01
    assert store.calls[0][2] == "2026-07-01T00:00:00Z"


def test_check_insights_empty_when_no_store():
    orch = _orch_with_store(None)
    out = orch.check_insights("o/n", "7d")
    assert out == {"repo": "o/n", "window": "7d", "slowest": [], "flakiest": [],
                   "regressions": [], "runner_wait_health": []}


def test_check_insights_unknown_window_clamps_to_7d():
    store = _FakeStore()
    out = _orch_with_store(store).check_insights("o/n", "bogus")
    assert out["window"] == "7d"


def test_check_insights_includes_regressions_and_runner_waits(monkeypatch):
    from datetime import datetime, timezone

    class _Reg:
        def to_dict(self):
            return {"check_name": "c", "event": "push", "prior_p50": 30.0,
                    "recent_p50": 120.0, "ratio": 4.0, "since": "2026-06-21T00:00:00Z"}

    class _RW:
        def to_dict(self):
            return {"event": "push", "p50": 10.0, "p90": 50.0, "prev_p50": 8.0, "n": 5}

    seen = {}

    class _Store:
        def slowest_checks(self, repo, since):
            seen["since"] = since
            return []

        def flakiest_checks(self, repo, since):
            return []

        def duration_regressions(self, repo):
            seen["reg_repo"] = repo
            return [_Reg()]

        def runner_wait_health(self, repo, since, prev_since):
            seen["prev_since"] = prev_since
            return [_RW()]

    orch = _orch_with_store(_Store())
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    out = orch.check_insights("o/r", "7d", now=now)

    assert out["regressions"][0]["ratio"] == 4.0
    assert out["runner_wait_health"][0]["event"] == "push"
    # window=7d → since = now-7d, prev_since = now-14d
    assert seen["since"] == "2026-07-01T00:00:00Z"
    assert seen["prev_since"] == "2026-06-24T00:00:00Z"
    assert seen["reg_repo"] == "o/r"


def test_check_insights_empty_payload_has_new_keys():
    orch = _orch_with_store(None)  # no store
    out = orch.check_insights("o/r", "7d")
    assert out["regressions"] == [] and out["runner_wait_health"] == []
