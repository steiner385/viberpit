# server/tests/test_merge_train_history.py
from server.github.merge_queue import QueueEntry
from server.pipeline.merge_train import Observation, derive_history, derive_merge_train_state

def _e(num, checks, state="AWAITING_CHECKS", oid="h"):
    return QueueEntry(pr_number=num, position=1, state=state, enqueued_at=0,
                      batch_head_oid=oid, group_checks=checks, checks_total=4,
                      checks_done=None, eta_seconds=None)

def test_eject_when_pr_departs_with_failing_checks():
    obs = [
        Observation(0, (_e(1, "failing", oid="a"), _e(2, "pending", oid="b"))),
        Observation(60, (_e(2, "pending", oid="b"),)),  # PR 1 departed while failing → eject
    ]
    h = derive_history(obs, now=60)
    assert h["ejects_24h"] == 1

def test_merged_batch_vs_failed_batch():
    obs = [
        Observation(0, (_e(1, "passing", oid="ok"), _e(2, "failing", oid="bad"))),
        Observation(60, ()),  # both departed: 'ok' merged, 'bad' ejected/failed
    ]
    h = derive_history(obs, now=60)
    assert h["ejects_24h"] == 1
    assert h["batch_success_pct"] == 50.0      # 1 merged / (1 merged + 1 failed)

def test_pr_still_present_is_not_a_departure():
    obs = [Observation(0, (_e(1, "pending"),)), Observation(60, (_e(1, "pending"),))]
    h = derive_history(obs, now=60)
    assert h["ejects_24h"] == 0
    assert h["batch_success_pct"] is None      # no completed batches

def test_window_excludes_old_observations():
    obs = [
        Observation(0, (_e(1, "passing", oid="old"),)),
        Observation(100, ()),                          # departure at t=100
    ]
    # now far ahead; cutoff drops both -> no history
    h = derive_history(obs, now=1_000_000, window_seconds=86400)
    assert h["ejects_24h"] == 0

def test_derive_full_state_idle_when_empty():
    st = derive_merge_train_state("r", [], now=10, awaiting_qa=0, awaiting_prod=0)
    assert st.depth == 0
    assert st.is_idle()

def test_derive_full_state_carries_backlog_and_live():
    obs = [Observation(10, (_e(99, "pending", state="AWAITING_CHECKS", oid="z"),))]
    st = derive_merge_train_state("r", obs, now=10, awaiting_qa=1, awaiting_prod=42)
    assert st.depth == 1
    assert st.awaiting_qa == 1 and st.awaiting_prod == 42
    assert st.merges_next is not None and st.merges_next.pr_number == 99
