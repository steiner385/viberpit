import json
from server.sessions.background_tasks import (
    scan_open_background_tasks, scan_pending_wakeup, is_parked, build_rearm_prompt,
    OpenBgTask,
)


def _notif(tuid, status):
    content = (f"<task-notification>\n<task-id>t</task-id>\n"
               f"<tool-use-id>{tuid}</tool-use-id>\n<status>{status}</status>\n"
               f"<summary>s</summary>\n</task-notification>")
    return {"type": "queue-operation", "operation": "enqueue", "content": content}


def _assistant_tool(tid, name, inp):
    return {"type": "assistant", "uuid": "u",
            "message": {"model": "claude-opus-4-8",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _user(text, *, is_meta=False):
    """A real user turn boundary (is_meta=True models a fired /loop wakeup re-injection)."""
    return {"type": "user", "isMeta": is_meta,
            "message": {"role": "user", "content": text}}


def _write(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    return str(p)


def test_detects_monitor_and_background_bash_ignores_foreground(tmp_path):
    path = _write(tmp_path, [
        _assistant_tool("toolu_mon", "Monitor",
                        {"description": "Watch PR 10604", "persistent": True,
                         "timeout_ms": 3600000, "command": "while true; do :; done"}),
        _assistant_tool("toolu_bg", "Bash",
                        {"command": "gh pr view 263", "description": "Poll PR",
                         "run_in_background": True}),
        _assistant_tool("toolu_fg", "Bash",
                        {"command": "ls", "description": "list"}),  # foreground → ignored
    ])
    tasks = scan_open_background_tasks(path)
    by_id = {t.tool_use_id: t for t in tasks}
    assert set(by_id) == {"toolu_mon", "toolu_bg"}
    assert by_id["toolu_mon"].kind == "monitor"
    assert by_id["toolu_bg"].kind == "task"
    assert by_id["toolu_mon"].description == "Watch PR 10604"


def test_completed_or_failed_closes_task_but_killed_and_events_do_not(tmp_path):
    path = _write(tmp_path, [
        _assistant_tool("toolu_done", "Bash", {"command": "x", "run_in_background": True}),
        _notif("toolu_done", "completed"),
        _assistant_tool("toolu_failed", "Bash", {"command": "y", "run_in_background": True}),
        _notif("toolu_failed", "failed"),
        _assistant_tool("toolu_killed", "Monitor", {"description": "m", "command": "z"}),
        _notif("toolu_killed", "killed"),     # killed is NOT terminal for the scan
    ])
    ids = {t.tool_use_id for t in scan_open_background_tasks(path)}
    assert ids == {"toolu_killed"}


def test_dedups_by_tool_use_id_and_tolerates_garbage(tmp_path):
    path = _write(tmp_path, [
        _assistant_tool("toolu_mon", "Monitor", {"description": "m", "command": "z"}),
    ])
    # append a malformed line + a duplicate launch with the same id
    with open(path, "a") as f:
        f.write("not json\n")
        f.write(json.dumps(_assistant_tool("toolu_mon", "Monitor",
                                           {"description": "m", "command": "z"})) + "\n")
    tasks = scan_open_background_tasks(path)
    assert [t.tool_use_id for t in tasks] == ["toolu_mon"]


def test_pending_wakeup_armed_when_last_turn_scheduled(tmp_path):
    # Agent's most recent turn called ScheduleWakeup and stopped → still waiting for it.
    path = _write(tmp_path, [
        _user("poll the PRs until they merge"),
        _assistant_tool("toolu_w1", "ScheduleWakeup",
                        {"delaySeconds": 1200, "reason": "watching CI"}),
    ])
    assert scan_pending_wakeup(path) is True


def test_pending_wakeup_false_when_no_wakeup(tmp_path):
    path = _write(tmp_path, [
        _user("do a thing"),
        _assistant_tool("toolu_b", "Bash", {"command": "ls"}),
    ])
    assert scan_pending_wakeup(path) is False


def test_pending_wakeup_false_when_loop_ended_in_later_turn(tmp_path):
    # Wakeup armed, fired (meta re-injection), then the agent ended the loop by NOT
    # re-arming in its final turn → no longer pending.
    path = _write(tmp_path, [
        _user("watch the deploy"),
        _assistant_tool("toolu_w1", "ScheduleWakeup", {"delaySeconds": 1200, "reason": "x"}),
        _user("<<autonomous-loop-dynamic>>", is_meta=True),          # wakeup fired
        _assistant_tool("toolu_done", "Bash", {"command": "echo done"}),  # no re-arm
    ])
    assert scan_pending_wakeup(path) is False


def test_pending_wakeup_true_when_rearmed_after_firing(tmp_path):
    # Loop continues: the post-fire turn re-arms the wakeup → still pending.
    path = _write(tmp_path, [
        _user("watch the deploy"),
        _assistant_tool("toolu_w1", "ScheduleWakeup", {"delaySeconds": 1200, "reason": "x"}),
        _user("<<autonomous-loop-dynamic>>", is_meta=True),          # wakeup fired
        _assistant_tool("toolu_w2", "ScheduleWakeup", {"delaySeconds": 1200, "reason": "x"}),
    ])
    assert scan_pending_wakeup(path) is True


def test_pending_wakeup_false_on_missing_file(tmp_path):
    assert scan_pending_wakeup(str(tmp_path / "nope.jsonl")) is False


def test_is_parked_true_for_open_background_task(tmp_path):
    path = _write(tmp_path, [
        _assistant_tool("toolu_bg", "Bash", {"command": "gh pr view 263", "run_in_background": True}),
    ])
    assert is_parked(path) is True


def test_is_parked_true_for_armed_wakeup(tmp_path):
    path = _write(tmp_path, [
        _user("poll the PRs"),
        _assistant_tool("toolu_w", "ScheduleWakeup", {"delaySeconds": 1200, "reason": "x"}),
    ])
    assert is_parked(path) is True


def test_is_parked_false_when_neither(tmp_path):
    path = _write(tmp_path, [
        _user("do a thing"),
        _assistant_tool("toolu_fg", "Bash", {"command": "ls"}),
    ])
    assert is_parked(path) is False


def test_is_parked_false_for_killed_task(tmp_path):
    # A background poller killed by a deploy restart is NOT a live external wait:
    # it is dead, and is re-armed (under a NEW tool-use-id) from the pre-kill
    # `pending_rearm` snapshot, not from this scan. Counting the dead killed id as
    # "open" pins the session on `waiting_external` forever (the real bug). The
    # re-arm scan still keeps killed tasks — see the test below.
    path = _write(tmp_path, [
        _assistant_tool("toolu_killed", "Bash", {"command": "gh pr view 10616", "run_in_background": True}),
        _notif("toolu_killed", "killed"),
    ])
    assert is_parked(path) is False


def test_killed_task_still_open_for_rearm_snapshot(tmp_path):
    # The re-arm path (default) MUST still see a killed task so it can relaunch it.
    path = _write(tmp_path, [
        _assistant_tool("toolu_killed", "Bash", {"command": "x", "run_in_background": True}),
        _notif("toolu_killed", "killed"),
    ])
    assert {t.tool_use_id for t in scan_open_background_tasks(path)} == {"toolu_killed"}
    assert scan_open_background_tasks(path, treat_killed_as_open=False) == []


def test_is_parked_false_on_missing_path(tmp_path):
    assert is_parked(str(tmp_path / "nope.jsonl")) is False
    assert is_parked(None) is False


def test_build_rearm_prompt_guards_only_non_monitor():
    mon = OpenBgTask("toolu_mon", "monitor", "Monitor", {"command": "z"}, "Watch PR")
    bash = OpenBgTask("toolu_bg", "task", "Bash", {"command": "x", "run_in_background": True}, "Poll")
    guard = "interrupted by a server restart"
    assert guard not in build_rearm_prompt([mon])
    p = build_rearm_prompt([mon, bash])
    assert guard in p
    assert '"command": "z"' in p and '"command": "x"' in p   # verbatim input JSON
