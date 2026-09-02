from server.sessions.types import SessionRecord, RESUME_ELIGIBLE_STATUSES


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", claude_session_id="c",
                cols=80, rows=24, cwd="/tmp", repo=None)
    base.update(kw)
    return SessionRecord(**base)  # type: ignore[arg-type]


def test_eligible_statuses_are_work_states_only():
    assert RESUME_ELIGIBLE_STATUSES == frozenset({"in_progress", "working", "condensing"})


def test_resumable_true_only_for_flagged_work_state():
    r = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    assert r.to_dict()["resumable"] is True


def test_resumable_false_when_from_status_not_work():
    r = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="ready_review")
    assert r.to_dict()["resumable"] is False


def test_resumable_false_without_flag():
    r = _rec(status="in_progress", interrupted_by_restart=False, interrupted_from_status="in_progress")
    assert r.to_dict()["resumable"] is False


def test_interrupted_from_status_round_trips():
    r = _rec(interrupted_by_restart=True, interrupted_from_status="working")
    assert SessionRecord.from_dict(r.to_dict()).interrupted_from_status == "working"
