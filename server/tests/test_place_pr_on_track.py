from server.github.pr_status import PullRequestInfo
from server.pipeline.track import build_stage_model, place_pr_on_track


def _states(pr, model):
    return {s.id: s.state for s in place_pr_on_track(pr, model)}


def _pr(**kw):
    base = dict(number=1, url="u", state="open", checks="none", title="t")
    base.update(kw)
    return PullRequestInfo(**base)


def test_ci_pending_pr_lights_ci_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=[])
    st = _states(_pr(stage="ci", checks="pending"), m)
    assert st == {"ci": "active", "ready": "pending", "merged": "pending"}


def test_parked_failing_pr_lights_ci_failed():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=[])
    st = _states(_pr(stage="parked", checks="failing"), m)
    assert st["ci"] == "failed"


def test_ready_pr_ci_done_ready_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=[])
    st = _states(_pr(stage="ready", checks="passing"), m)
    assert st == {"ci": "done", "ready": "active", "merged": "pending"}


def test_merged_pr_all_done():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=["production"])
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=True), m)
    assert st == {"ci": "done", "ready": "done", "deploy:production": "done", "merged": "done"}


def test_merged_not_deployed_leaves_deploy_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=["production"])
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=False), m)
    assert st["deploy:production"] == "active"
    assert st["merged"] == "pending"


def test_queue_pr_active_on_queue_node():
    m = build_stage_model("o/r", ci_graph=None, has_queue=True, deploy_envs=[])
    st = _states(_pr(stage="queue", checks="passing"), m)
    assert st == {"ci": "done", "ready": "done", "queue": "active", "merged": "pending"}


def test_queue_pr_failing_lights_queue_failed():
    m = build_stage_model("o/r", ci_graph=None, has_queue=True, deploy_envs=[])
    st = _states(_pr(stage="queue", checks="failing"), m)
    assert st["queue"] == "failed"


def test_merged_pr_no_deploy_node_merged_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=[])
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=False), m)
    assert st == {"ci": "done", "ready": "done", "merged": "active"}
