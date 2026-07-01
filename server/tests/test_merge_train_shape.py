from server.pipeline.merge_train import MergeTrainState, TrainBuild


def test_to_dict_exact_shape():
    st = MergeTrainState(
        repo="cairnea/KinDash", depth=5, trains_per_hour=0.8, batch_success_pct=38.0,
        ejects_24h=46, oldest_wait_seconds=5760, failing=(10823,),
        building=TrainBuild(pr_number=10824, percent=47, eta_seconds=720),
        blocked=(10805, 10799, 10814), awaiting_qa=1, awaiting_prod=42,
        merges_next=TrainBuild(pr_number=10824, percent=47, eta_seconds=720),
    )
    assert st.to_dict() == {
        "repo": "cairnea/KinDash", "depth": 5, "trains_per_hour": 0.8,
        "batch_success_pct": 38.0, "ejects_24h": 46, "oldest_wait_seconds": 5760,
        "failing": [10823],
        "building": {"pr_number": 10824, "percent": 47, "eta_seconds": 720},
        "blocked": [10805, 10799, 10814], "awaiting_qa": 1, "awaiting_prod": 42,
        "merges_next": {"pr_number": 10824, "percent": 47, "eta_seconds": 720},
    }


def test_from_dict_round_trips():
    st = MergeTrainState(
        repo="r", depth=0, trains_per_hour=None, batch_success_pct=None, ejects_24h=0,
        oldest_wait_seconds=None, failing=(), building=None, blocked=(),
        awaiting_qa=0, awaiting_prod=0, merges_next=None)
    assert MergeTrainState.from_dict(st.to_dict()) == st
