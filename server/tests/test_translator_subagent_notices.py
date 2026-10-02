"""Task sub-agent notices (task_started / task_progress / task_updated / task_notification)
become SubAgentStarted / SubAgentProgress / SubAgentEnded, tagged with the spawning Task's
tool-use id. Shapes verified against claude 2.1.268 on 2026-09-11 (spec §2)."""
from server.sessions import events as ev
from server.sessions.translator import Translator
from server.tests.fakes import fake_sdk as f

TID = "toolu_01YNkJcCo55UaJospWhvnJMx"
TASK = "abb42ada82e1382fe"


def _run(msgs):
    tr = Translator(); out = []
    for m in msgs:
        out += tr.feed(m)
    return tr, out


def test_task_started_emits_sub_agent_started():
    _, out = _run([f.task_started(TASK, TID, description="sleep probe",
                                  subagent_type="Explore", backgrounded=True, depth=2)])
    assert len(out) == 1
    e = out[0]
    assert isinstance(e, ev.SubAgentStarted)
    assert (e.parent_tool_use_id, e.task_id, e.description, e.agent_type) == (
        TID, TASK, "sleep probe", "Explore")
    assert e.backgrounded is True and e.depth == 2
    w = e.to_wire()
    assert w["type"] == "subAgentStarted" and w["parentToolUseId"] == TID
    assert w["taskId"] == TASK and w["agentType"] == "Explore" and w["backgrounded"] is True
    assert isinstance(w["ts"], float)


def test_task_started_spawn_depth_zero_is_not_conflated_with_absent():
    """#1562: `int(d.get("spawn_depth") or 1)` treated an explicit 0 the same as a
    missing field — both collapsed to 1. A real spawn_depth of 0 must survive as 0;
    only an actually-absent field falls back to the depth=1 default."""
    _, out = _run([f.task_started(TASK, TID, depth=0)])
    assert out[0].depth == 0


def test_task_started_missing_spawn_depth_defaults_to_one():
    from claude_agent_sdk import TaskStartedMessage
    data = {"type": "system", "subtype": "task_started", "task_id": TASK,
            "description": "sleep probe", "subagent_type": "general-purpose",
            "is_backgrounded": False, "task_type": "local_agent",
            "uuid": "ts1", "session_id": "sess-1", "tool_use_id": TID}
    msg = TaskStartedMessage(subtype="task_started", data=data, task_id=TASK,
                             description="sleep probe", uuid="ts1", session_id="sess-1",
                             tool_use_id=TID, task_type="local_agent")
    _, out = _run([msg])
    assert out[0].depth == 1


def test_task_started_without_tool_use_id_is_ignored():
    """A Bash/Monitor task has no spawning Task call — nothing to attach it to."""
    _, out = _run([f.task_started(TASK, None, task_type="local_bash")])
    assert out == []


def test_local_bash_task_started_becomes_background_task_started():
    """A backgrounded Bash / Monitor is a `local_bash` task whose task_started carries the
    Bash call's tool_use_id (claude 2.1.287, read from the binary 2026-10-02). It is not a
    sub-agent — it gets its own event (#2635)."""
    _, out = _run([f.task_started(TASK, TID, description="pytest -q", task_type="local_bash",
                                  backgrounded=True)])
    assert len(out) == 1 and isinstance(out[0], ev.BackgroundTaskStarted)
    e = out[0]
    assert (e.tool_use_id, e.task_id, e.description, e.task_type, e.backgrounded) == (
        TID, TASK, "pytest -q", "local_bash", True)
    w = e.to_wire()
    assert w["type"] == "backgroundTaskStarted" and w["toolUseId"] == TID
    assert w["taskType"] == "local_bash" and w["backgrounded"] is True and isinstance(w["ts"], float)


def test_background_task_notification_resolves_tool_use_id_from_the_map():
    """The CLI's task_notification carries no task_type; the map started by task_started
    is what says this is a shell, even when the notice itself names the tool_use_id."""
    _, out = _run([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True),
                   f.task_notification(TASK, None, status="completed", summary="ok")])
    e = out[1]
    assert isinstance(e, ev.BackgroundTaskEnded)
    assert (e.tool_use_id, e.task_id, e.status) == (TID, TASK, "completed")
    assert e.to_wire() == {"type": "backgroundTaskEnded", "toolUseId": TID, "taskId": TASK,
                           "status": "completed", "ts": e.ts}


def test_background_kill_sequence_emits_one_ended_per_frame_but_evicts_once():
    tr, out = _run([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True),
                    f.task_updated(TASK, "killed"),
                    f.task_notification(TASK, TID, status="stopped", summary="")])
    ended = [e for e in out if isinstance(e, ev.BackgroundTaskEnded)]
    assert [e.status for e in ended] == ["stopped", "stopped"]
    assert TASK not in tr._bg_task_tool


def test_background_progress_emits_nothing():
    _, out = _run([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True),
                   f.task_progress(TASK, TID)])
    assert len(out) == 1


def test_agent_task_path_is_unchanged_by_the_background_map():
    """A local_agent still becomes SubAgentStarted and never touches _bg_task_tool."""
    tr, out = _run([f.task_started(TASK, TID)])
    assert isinstance(out[0], ev.SubAgentStarted) and tr._bg_task_tool == {}


def test_progress_resolves_tool_use_id_from_the_map():
    _, out = _run([f.task_started(TASK, TID), f.task_progress(TASK, None, activity="Reading files",
                                                                total_tokens=7, tool_uses=2,
                                                                duration_ms=99, last_tool="Read")])
    p = out[1]
    assert isinstance(p, ev.SubAgentProgress)
    assert p.parent_tool_use_id == TID and p.activity == "Reading files"
    assert (p.total_tokens, p.tool_uses, p.duration_ms, p.last_tool) == (7, 2, 99, "Read")
    w = p.to_wire()
    assert w["type"] == "subAgentProgress" and w["lastTool"] == "Read" and w["totalTokens"] == 7


def test_progress_for_unknown_task_is_dropped():
    _, out = _run([f.task_progress("nope", None)])
    assert out == []


def test_updated_killed_maps_to_stopped_and_notification_repeats_ended_with_summary():
    """The real kill sequence: task_updated(killed) then task_notification(stopped).
    Both emit Ended (the client merges idempotently); the second carries the summary."""
    tr = Translator()
    out = tr.feed(f.task_started(TASK, TID))
    out += tr.feed(f.task_updated(TASK, "killed"))
    # S2: the map is evicted on the terminal task_updated, not only on the notification
    # that follows — the SDK docs allow that notification to never arrive at all.
    assert TASK not in tr._task_tool
    # The later notification still emits Ended: it carries its own tool_use_id, so it
    # does not need the (already-evicted) map entry to resolve.
    out += tr.feed(f.task_notification(TASK, TID, status="stopped", summary="sleep probe",
                                       usage={"total_tokens": 5, "tool_uses": 1, "duration_ms": 3}))
    ends = [e for e in out if isinstance(e, ev.SubAgentEnded)]
    assert [e.status for e in ends] == ["stopped", "stopped"]
    assert ends[0].parent_tool_use_id == TID and ends[0].summary == ""
    assert ends[1].summary == "sleep probe" and ends[1].total_tokens == 5
    assert ends[1].to_wire()["type"] == "subAgentEnded"


def test_updated_non_terminal_status_emits_nothing():
    _, out = _run([f.task_started(TASK, TID), f.task_updated(TASK, "running")])
    assert len(out) == 1


def test_notification_drops_the_map_entry():
    tr, _ = _run([f.task_started(TASK, TID), f.task_notification(TASK, TID, status="completed")])
    assert TASK not in tr._task_tool
    # A late progress frame for a finished task has nowhere to go.
    assert tr.feed(f.task_progress(TASK, None)) == []


def test_notification_with_tool_use_id_needs_no_prior_start():
    """A daemon that attached mid-run never saw task_started; the notification names
    the Task call itself, so the end still lands on the right thread."""
    _, out = _run([f.task_notification(TASK, TID, status="failed", summary="boom")])
    assert len(out) == 1 and isinstance(out[0], ev.SubAgentEnded)
    assert out[0].parent_tool_use_id == TID and out[0].status == "failed"


def test_notification_non_agent_type_with_tool_use_id_is_ignored():
    """A local_bash/monitor notification that happens to carry a tool_use_id (and has
    no prior start) must not be mistaken for a Task sub-agent ending."""
    _, out = _run([f.task_notification(TASK, TID, status="completed",
                                       task_type="local_bash")])
    assert out == []


def test_map_survives_turn_end():
    """Background sub-agents outlive the turn that spawned them: their notices arrive
    while the driver is idle. `_reset()` (the per-turn reset on ResultMessage) never
    clears the map — it lives for the translator's lifetime, not the turn."""
    tr, _ = _run([f.task_started(TASK, TID), f.stream_start(uuid="t1"), f.result(uuid="t1")])
    assert tr._task_tool == {TASK: TID}
