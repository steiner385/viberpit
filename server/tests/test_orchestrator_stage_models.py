"""Task 5: SessionOrchestrator.all_repos_stage_models() + node_states in all_prs()."""
from server.github.pr_status import PullRequestInfo
from server.pipeline.track import StageModel


class _FakeStore:
    """Minimal HistoryStore fake: ci_graph always returns None."""
    def ci_graph(self, repo):
        return None


class _FakePop:
    def __init__(self, data):
        self._d = data

    def all(self):
        return self._d

    def history_store(self):
        return _FakeStore()


def _mk_orch(population):
    from server.sessions import orchestrator as om
    orch = object.__new__(om.SessionOrchestrator)
    orch._pr_population = _FakePop(population)
    orch._records = {}
    return orch


def test_all_repos_stage_models_per_repo():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending", title="t", repo="o/r", stage="ci")
    orch = _mk_orch({"o/r": (pr,)})
    models = orch.all_repos_stage_models()
    assert isinstance(models["o/r"], StageModel)
    assert [n.id for n in models["o/r"].nodes] == ["ci", "ready", "merged"]


def test_all_repos_stage_models_adds_queue_and_deploy():
    q = PullRequestInfo(number=2, url="u", state="open", checks="none", title="t", repo="o/r", stage="queue")
    d = PullRequestInfo(number=3, url="u", state="merged", checks="passing", title="t", repo="o/r", stage="merged", deployed=True)
    orch = _mk_orch({"o/r": (q, d)})
    models = orch.all_repos_stage_models()
    assert [n.id for n in models["o/r"].nodes] == ["ci", "ready", "queue", "deploy:production", "merged"]


def test_all_prs_attaches_node_states():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending", title="t", repo="o/r", stage="ci")
    orch = _mk_orch({"o/r": (pr,)})
    items = orch.all_prs()
    assert items[0].node_states  # non-empty
    assert {s.id: s.state for s in items[0].node_states}["ci"] == "active"
