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


def _mk_orch(monkeypatch_or_population, population=None):
    # Support both old call style _mk_orch(population) and new _mk_orch(monkeypatch, population)
    if population is None:
        population = monkeypatch_or_population
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
    d = PullRequestInfo(number=3, url="u", state="merged", checks="passing", title="t", repo="o/r", stage="merged",
                        deployed=True, deployed_envs=("production",))
    orch = _mk_orch({"o/r": (q, d)})
    models = orch.all_repos_stage_models()
    assert [n.id for n in models["o/r"].nodes] == ["ci", "ready", "queue", "merged", "deploy:production"]


def test_all_prs_attaches_node_states():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending", title="t", repo="o/r", stage="ci")
    orch = _mk_orch({"o/r": (pr,)})
    items = orch.all_prs()
    assert items[0].node_states  # non-empty
    assert {s.id: s.state for s in items[0].node_states}["ci"] == "active"


def test_all_repos_stage_models_builds_deploy_nodes_from_deployed_envs(monkeypatch):
    p1 = PullRequestInfo(number=1, url="u", state="merged", checks="passing", title="t",
                         repo="o/r", stage="merged", deployed_envs=("staging",))
    p2 = PullRequestInfo(number=2, url="u", state="merged", checks="passing", title="t",
                         repo="o/r", stage="merged", deployed_envs=("production", "qa"))
    orch = _mk_orch(monkeypatch, {"o/r": (p1, p2)})
    models = orch.all_repos_stage_models()
    ids = [n.id for n in models["o/r"].nodes]
    # union {staging, production, qa} ordered → qa, staging, production
    assert ids == ["ci", "ready", "merged", "deploy:qa", "deploy:staging", "deploy:production"]
