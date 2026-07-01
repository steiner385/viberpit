from server.github.merge_queue import QueueEntry
from server.pipeline.merge_train import Observation, TrainBuild, derive_live


def _e(num, pos, state, checks, oid="h", enq=0, total=4, eta=None):
    return QueueEntry(pr_number=num, position=pos, state=state, enqueued_at=enq,
                      batch_head_oid=oid, group_checks=checks, checks_total=total,
                      checks_done=None, eta_seconds=eta)


def test_merges_next_omits_progress_when_head_blocked_or_failing():
    now = 100
    # Head is UNMERGEABLE + failing: identify it, but don't imply a % or an ETA
    # it will never meet (seen live: #10805 showed "100% ~33m" while blocked+failing).
    blocked_head = Observation(now, (_e(10805, 1, "UNMERGEABLE", "failing", eta=1998),))
    assert derive_live(blocked_head, now)["merges_next"] == TrainBuild(10805, None, None)

    # A healthy (pending) head keeps its ETA.
    ok_head = Observation(now, (_e(10824, 1, "AWAITING_CHECKS", "pending", eta=720),))
    mn = derive_live(ok_head, now)["merges_next"]
    assert mn.pr_number == 10824 and mn.eta_seconds == 720


def test_live_segments():
    now = 10_000
    latest = Observation(captured_at=now, entries=(
        _e(10824, 1, "AWAITING_CHECKS", "pending", oid="a", enq=now - 5760, eta=720),
        _e(10823, 2, "AWAITING_CHECKS", "failing", oid="b", enq=now - 100),
        _e(10805, 3, "UNMERGEABLE", None, oid="c", enq=now - 200),
    ))
    out = derive_live(latest, now)
    assert out["depth"] == 3
    assert out["oldest_wait_seconds"] == 5760            # min(enqueued_at)
    assert out["failing"] == (10823,)
    assert out["blocked"] == (10805,)
    assert out["merges_next"] == TrainBuild(10824, None, 720)   # head of queue
    assert out["building"] == TrainBuild(10824, None, 720)      # head batch is running


def test_live_building_none_when_head_not_running():
    now = 5
    latest = Observation(captured_at=now, entries=(
        _e(1, 1, "MERGEABLE", "passing", eta=0),
    ))
    out = derive_live(latest, now)
    assert out["building"] is None


def test_live_empty_observation():
    out = derive_live(Observation(captured_at=1, entries=()), now=1)
    assert out == {"depth": 0, "oldest_wait_seconds": None, "failing": (),
                   "building": None, "blocked": (), "merges_next": None}
