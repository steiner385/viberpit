import json
from datetime import datetime, timezone
from server.sessions.background_tasks import (
    scan_open_background_tasks, scan_pending_wakeup, is_parked, build_rearm_prompt,
    last_assistant_text, text_awaits_user, OpenBgTask,
)


def last_turn_awaits_user(jsonl_path):
    """The composition ``_persist_status`` performs for a CLAUDE session.

    Was a single ``background_tasks`` helper until #1284 made the prose source
    engine-neutral: the orchestrator now reads the last assistant message through
    ``_last_assistant_prose`` (transcript for Claude, driver turn buffer for Codex)
    and judges it with ``text_awaits_user``. Composing the two here keeps these cases
    pinned on the Claude path they were written for.
    """
    if not jsonl_path:
        return False
    return text_awaits_user(last_assistant_text(jsonl_path))


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


def _assistant_text(text):
    """An assistant turn that ends in prose (a hand-back message, no tool_use)."""
    return {"type": "assistant", "uuid": "u",
            "message": {"model": "claude-opus-4-8",
                        "content": [{"type": "text", "text": text}]}}


def _tool_result(tid, text="ok"):
    """The CLI's journal entry for a tool's result: a ``type:"user"`` record carrying a
    ``tool_result`` block plus a top-level ``toolUseResult`` — the exact shape observed
    in a live transcript (dobby, 2026-09-07). Mid-turn: nobody typed it and the
    assistant keeps going after it."""
    return {"type": "user", "toolUseResult": text,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": text}]}}


def _ack(tid, tool_use_result, text="ok"):
    """The CLI's journal entry for a tool result whose top-level ``toolUseResult`` is
    the CLI's own STRUCTURED record of what the tool did (a dict), as observed for the
    Agent / SendMessage / backgrounded-Bash tools on dobby, 2026-09-07."""
    return {"type": "user", "toolUseResult": tool_use_result,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": [{"type": "text", "text": text}]}]}}


# The CLI's ack for an Agent launched in the background (observed shape). The launch
# input carries NO ``run_in_background`` key — background is the tool's default — so
# the ack, not the input, is what says the agent is still running.
_ASYNC_ACK = {"isAsync": True, "status": "async_launched", "agentId": "a44a48abd0bd2ac9c",
              "description": "Map surfaces", "resolvedModel": "claude-opus-5"}
# Same tool, run in the foreground: the ack IS the agent's report.
_SYNC_ACK = {"status": "completed", "prompt": "…", "totalDurationMs": 240000}


def _task_notif(task_id, status, tuid=None):
    """A ``<task-notification>`` keyed by task-id, with or without a tool-use-id. The
    CLI's post-restart "no completion record" notice (``stopped``) carries only the
    task-id."""
    tu = f"<tool-use-id>{tuid}</tool-use-id>\n" if tuid else ""
    content = (f"<task-notification>\n<task-id>{task_id}</task-id>\n{tu}"
               f"<output-file>/tmp/x.output</output-file>\n<status>{status}</status>\n"
               f"<summary>s</summary>\n</task-notification>")
    return {"type": "user", "message": {"role": "user", "content": content}}


def _write(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    return str(p)


def test_background_agent_is_open_work_when_its_ack_says_async_launched(tmp_path):
    # The real 2026-09-07 shape (sessions 263e890f, 535694fe, 86b5b092, ea021e6c):
    # Agent launched with no run_in_background key, the CLI acks "async_launched",
    # the parent closes its turn with "I'll draft the design once that report lands."
    # Those sessions sat on "Your turn" while several subagents were visibly working.
    path = _write(tmp_path, [
        _user("design the monitor"),
        _assistant_tool("toolu_ag", "Agent",
                        {"subagent_type": "Explore", "description": "Map surfaces",
                         "prompt": "Map the error surfaces"}),
        _ack("toolu_ag", _ASYNC_ACK, "Async agent launched successfully."),
        _assistant_text("I'll draft the design once that report lands."),
    ])
    tasks = scan_open_background_tasks(path)
    assert [t.tool_use_id for t in tasks] == ["toolu_ag"]
    assert tasks[0].kind == "agent"
    assert tasks[0].description == "Map surfaces"
    assert is_parked(path) is True


def test_foreground_agent_ack_is_its_report_not_open_work(tmp_path):
    # run_in_background absent AND the ack is the finished report → nothing is open.
    # (An older CLI whose Agent defaulted to foreground journals exactly this.)
    path = _write(tmp_path, [
        _assistant_tool("toolu_fg", "Agent",
                        {"subagent_type": "general-purpose", "description": "d", "prompt": "p"}),
        _ack("toolu_fg", _SYNC_ACK, "- **Status:** DONE"),
        _assistant_tool("toolu_fg2", "Agent",
                        {"subagent_type": "general-purpose", "description": "d", "prompt": "p",
                         "run_in_background": False}),
        _ack("toolu_fg2", _SYNC_ACK, "- **Status:** DONE"),
    ])
    assert scan_open_background_tasks(path) == []
    assert is_parked(path) is False


def test_agent_with_no_ack_yet_is_not_open_work(tmp_path):
    # Mid-turn: the tool_use is journaled before its result. Without the ack we do
    # not know it is background — and the turn is still active anyway, so the
    # dashboard is in_progress regardless. Never guess "parked" from the input.
    path = _write(tmp_path, [
        _assistant_tool("toolu_ag", "Agent", {"subagent_type": "Explore", "prompt": "p"}),
    ])
    assert scan_open_background_tasks(path) == []


def test_background_agent_closed_by_its_completion_notification(tmp_path):
    path = _write(tmp_path, [
        _assistant_tool("toolu_ag", "Agent", {"subagent_type": "Explore", "prompt": "p"}),
        _ack("toolu_ag", _ASYNC_ACK),
        _assistant_text("Waiting for its report."),
        _task_notif("a44a48abd0bd2ac9c", "completed", tuid="toolu_ag"),
    ])
    assert scan_open_background_tasks(path) == []
    assert is_parked(path) is False


def test_stopped_notification_keyed_by_task_id_only_closes_the_agent(tmp_path):
    # Observed after a daemon restart (86b5b092, 12:08): the CLI journals a
    # ``stopped`` notice for a background agent with no completion record, carrying
    # ONLY the task-id (the agentId from the launch ack). That agent is gone; it must
    # close in BOTH scans, or the session pins on "Waiting…" and the re-arm prompt
    # reports a task the CLI already told the model about.
    path = _write(tmp_path, [
        _assistant_tool("toolu_ag", "Agent", {"subagent_type": "Explore", "prompt": "p"}),
        _ack("toolu_ag", _ASYNC_ACK),
        _task_notif("a44a48abd0bd2ac9c", "stopped"),
    ])
    assert scan_open_background_tasks(path) == []
    assert scan_open_background_tasks(path, treat_killed_as_open=False) == []


def test_send_message_to_finished_agent_reopens_it_until_it_notifies_again(tmp_path):
    # 86b5b092 11:58: agent completed, parent re-dispatched it with SendMessage (ack
    # carries resumedAgentId), then closed its turn waiting. The next notification
    # names the SendMessage's own tool-use-id (and the same task-id).
    path = _write(tmp_path, [
        _assistant_tool("toolu_ag", "Agent", {"subagent_type": "general-purpose", "prompt": "p",
                                              "description": "Implement Task 1"}),
        _ack("toolu_ag", _ASYNC_ACK | {"agentId": "a2a352f"}),
        _task_notif("a2a352f", "completed", tuid="toolu_ag"),
        _assistant_tool("toolu_sm", "SendMessage",
                        {"to": "a2a352f", "summary": "Finish Task 1", "message": "collect it"}),
        _ack("toolu_sm", {"success": True, "message": "Resuming agent a2a352f",
                          "resumedAgentId": "a2a352f"}),
        _assistant_text("Nothing else is independent of that report, so I'm waiting for it."),
    ])
    tasks = scan_open_background_tasks(path)
    assert [t.tool_use_id for t in tasks] == ["toolu_sm"]
    assert tasks[0].kind == "agent"
    assert tasks[0].description == "Finish Task 1"
    assert is_parked(path) is True
    with open(path, "a") as f:
        f.write(json.dumps(_task_notif("a2a352f", "completed", tuid="toolu_sm")) + "\n")
    assert is_parked(path) is False


def test_background_bash_closes_on_task_id_only_notification(tmp_path):
    # A backgrounded Bash acks with ``backgroundTaskId``; a notification that names
    # only that task-id must still close it.
    path = _write(tmp_path, [
        _assistant_tool("toolu_bg", "Bash", {"command": "sleep 600", "run_in_background": True}),
        _ack("toolu_bg", {"stdout": "", "stderr": "", "backgroundTaskId": "b2k6gko2f"},
             "Command running in background with ID: b2k6gko2f."),
        _task_notif("b2k6gko2f", "completed"),
    ])
    assert scan_open_background_tasks(path) == []


def test_agents_are_reported_lost_on_rearm_never_relaunched(tmp_path):
    # A subagent's brief is a whole model-driven task that commits, pushes, opens
    # PRs — re-issuing one blindly after a restart duplicates all of that. Report it
    # like a Codex terminal; the parent still holds the brief if it wants it back.
    path = _write(tmp_path, [
        _assistant_tool("toolu_ag", "Agent", {"subagent_type": "general-purpose", "prompt": "p",
                                              "description": "Implement Task 3"}),
        _ack("toolu_ag", _ASYNC_ACK),
    ])
    [task] = scan_open_background_tasks(path)
    assert task.replayable is False
    prompt = build_rearm_prompt([task])
    assert "NOT restarted" in prompt and "Implement Task 3" in prompt
    assert "relaunch the Agent tool" not in prompt
    assert OpenBgTask.from_dict(task.to_dict()).replayable is False


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


def test_awaits_user_detects_prose_options(tmp_path):
    # The real scenario: agent finished a turn by presenting numbered options for the
    # user to choose — a hand-back with no structured AskUserQuestion.
    path = _write(tmp_path, [
        _user("plan the work"),
        _assistant_text(
            "Plan is ready.\n\nTwo execution options:\n"
            "1. Subagent-Driven (recommended) — fresh implementer per task.\n"
            "2. Inline Execution — I run the two tasks here with checkpoints."),
    ])
    assert last_turn_awaits_user(path) is True


def test_awaits_user_detects_trailing_question(tmp_path):
    path = _write(tmp_path, [
        _assistant_text("I can take approach A or B. Which would you prefer?"),
    ])
    assert last_turn_awaits_user(path) is True


def test_awaits_user_false_for_plain_report(tmp_path):
    path = _write(tmp_path, [
        _assistant_text("Done. Merged PR #10 and the deploy is green. "
                        "I'll keep monitoring the rollout in the background."),
    ])
    assert last_turn_awaits_user(path) is False


def test_awaits_user_uses_latest_assistant_text_only(tmp_path):
    # An earlier turn asked, but the latest turn is a plain report → not awaiting.
    path = _write(tmp_path, [
        _assistant_text("Which option do you want?"),
        _user("option 1"),
        _assistant_text("Got it — running option 1 now. Monitoring in the background."),
    ])
    assert last_turn_awaits_user(path) is False


def test_awaits_user_ignores_tool_use_only_final_turn(tmp_path):
    # A turn whose last assistant entry is a bare tool_use (e.g. an armed wakeup) has
    # no hand-back prose → not awaiting (must not mask the waiting_external overlay).
    path = _write(tmp_path, [
        _user("poll the PRs"),
        _assistant_tool("toolu_w1", "ScheduleWakeup",
                        {"delaySeconds": 1200, "reason": "watching CI"}),
    ])
    assert last_turn_awaits_user(path) is False


def test_awaits_user_none_path_is_false(tmp_path):
    assert last_turn_awaits_user(None) is False


def test_pending_wakeup_armed_when_last_turn_scheduled(tmp_path):
    # Agent's most recent turn called ScheduleWakeup and stopped → still waiting for it.
    path = _write(tmp_path, [
        _user("poll the PRs until they merge"),
        _assistant_tool("toolu_w1", "ScheduleWakeup",
                        {"delaySeconds": 1200, "reason": "watching CI"}),
    ])
    assert scan_pending_wakeup(path) is True


def test_pending_wakeup_armed_survives_its_own_tool_result(tmp_path):
    # The shape every real transcript has (dobby, 2026-09-07): the ScheduleWakeup
    # tool_use is followed by the CLI's tool_result entry — journaled as `type:"user"`
    # — and then the agent's closing prose. That tool_result is NOT a new turn: nobody
    # typed it. Treating it as one wiped the armed state, so a session parked on a
    # wakeup persisted `done` ("Your turn") with nothing for the user to do.
    path = _write(tmp_path, [
        _user("poll the PRs until they merge"),
        _assistant_tool("toolu_w1", "ScheduleWakeup",
                        {"delaySeconds": 1200, "reason": "watching CI"}),
        _tool_result("toolu_w1", "Wakeup scheduled in 1200s"),
        _assistant_text("No conflict to resolve. I'll confirm CI goes green on the next check-in."),
    ])
    assert scan_pending_wakeup(path) is True
    assert is_parked(path) is True


def test_pending_wakeup_false_when_fired_after_tool_result_and_not_rearmed(tmp_path):
    # Same real shape, then the wakeup fires (isMeta prompt re-injection) and the
    # post-fire turn does not re-arm → the loop ended, nothing pending. Guards that
    # ignoring tool_result entries does not also blind us to a real turn boundary.
    path = _write(tmp_path, [
        _user("watch the deploy"),
        _assistant_tool("toolu_w1", "ScheduleWakeup", {"delaySeconds": 1200, "reason": "x"}),
        _tool_result("toolu_w1"),
        _assistant_text("Waiting on CI."),
        _user("<<autonomous-loop-dynamic>>", is_meta=True),          # wakeup fired
        _assistant_tool("toolu_done", "Bash", {"command": "echo done"}),  # no re-arm
        _tool_result("toolu_done", "done"),
        _assistant_text("Merged; loop over."),
    ])
    assert scan_pending_wakeup(path) is False


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


def _wakeup_at(tid, ts, delay):
    o = _assistant_tool(tid, "ScheduleWakeup", {"delaySeconds": delay, "reason": "x"})
    o["timestamp"] = ts
    return o


def test_pending_wakeup_false_when_overdue_means_lost(tmp_path):
    # Armed at 12:00 with a 1200s (20min) delay → should have fired by 12:20. A wakeup
    # is not re-armed across a restart, so if no later turn exists well past its fire
    # time, the daemon missed it — it is lost, not pending.
    path = _write(tmp_path, [
        _user("watch the deploy"),
        _wakeup_at("toolu_w", "2026-06-24T12:00:00.000Z", 1200),
    ])
    now = datetime(2026, 6, 24, 13, 0, 0, tzinfo=timezone.utc)  # 60min later
    assert scan_pending_wakeup(path, now=now) is False


def test_pending_wakeup_true_when_not_yet_overdue(tmp_path):
    # Same wakeup, but "now" is before fire time + grace → still legitimately pending.
    path = _write(tmp_path, [
        _user("watch the deploy"),
        _wakeup_at("toolu_w", "2026-06-24T12:00:00.000Z", 1200),
    ])
    now = datetime(2026, 6, 24, 12, 10, 0, tzinfo=timezone.utc)  # 10min later, < 20min
    assert scan_pending_wakeup(path, now=now) is True


def test_pending_wakeup_true_when_armed_without_timestamp(tmp_path):
    # No timestamp to time it against → conservatively treat as pending (not lost).
    path = _write(tmp_path, [
        _user("watch the deploy"),
        _assistant_tool("toolu_w", "ScheduleWakeup", {"delaySeconds": 1200, "reason": "x"}),
    ])
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert scan_pending_wakeup(path, now=now) is True


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


def _tool_result_error(tid, text):
    """The CLI's journal entry for a tool call that was REFUSED — rejected on a
    permission card, or cut off by an interrupt ("The user doesn't want to proceed
    with this tool use"). Same ``type:"user"`` shape as a result, with ``is_error``."""
    return {"type": "user", "toolUseResult": text,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": text, "is_error": True}]}}


def test_refused_background_launch_is_not_open(tmp_path):
    """A ``run_in_background`` launch whose result is an error never became a task:
    nothing runs, no ``<task-notification>`` will ever close it. Counting it as open
    pinned a session on ``waiting_external`` ("Waiting…") for good (2026-09-16: an
    interrupt refused the PR-watcher launch, and the session read "Waiting…" while
    nothing at all was running). Terminal for both scans: not parked, not re-armed."""
    p = tmp_path / "t.jsonl"
    entries = [
        _user("watch the PRs"),
        _assistant_tool("tu-bg", "Bash", {"command": "./pr-waiter.sh",
                                          "run_in_background": True}),
        _tool_result_error("tu-bg", "The user doesn't want to proceed with this tool "
                                    "use. The tool use was rejected."),
    ]
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    assert scan_open_background_tasks(str(p), treat_killed_as_open=False) == []
    assert scan_open_background_tasks(str(p)) == []
    assert is_parked(str(p)) is False


def test_successful_background_launch_stays_open_until_its_notification(tmp_path):
    """Control for the above: the CLI's normal ack for a backgrounded Bash is NOT an
    error, and the launch stays open until its notification."""
    p = tmp_path / "t.jsonl"
    entries = [
        _user("watch the PRs"),
        _assistant_tool("tu-bg", "Bash", {"command": "./pr-waiter.sh",
                                          "run_in_background": True}),
        _ack("tu-bg", {"backgroundTaskId": "b1"}, "Command running in background"),
    ]
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    assert [t.tool_use_id for t in scan_open_background_tasks(str(p))] == ["tu-bg"]
    assert is_parked(str(p)) is True


def test_task_stopped_by_the_agent_is_closed_for_both_scans(tmp_path):
    # Session a50a5873 (2026-09-19): a `sleep 300; true` placeholder launched in the
    # background, then deliberately stopped with TaskStop. The CLI journals the stop
    # only as the TaskStop's own tool_result — never as a task-notification — so the
    # launch read as open forever: twelve hours on waiting_external, and four
    # post-restart prompts asking the agent to relaunch a task it had killed on purpose.
    stop_result = {"message": "Successfully stopped task: bmys2bjxs (sleep 300; true)",
                   "task_id": "bmys2bjxs", "task_type": "local_bash",
                   "command": "sleep 300; true"}
    path = _write(tmp_path, [
        _user("fix the failing CI"),
        _assistant_tool("toolu_bg", "Bash", {"command": "sleep 300; true",
                                             "run_in_background": True}),
        _ack("toolu_bg", {"stdout": "", "backgroundTaskId": "bmys2bjxs"}, ""),
        _assistant_tool("toolu_stop", "TaskStop", {"task_id": "bmys2bjxs"}),
        _ack("toolu_stop", stop_result, json.dumps(stop_result)),
        _assistant_text("Cancelled the placeholder; PR #160 merged, nothing outstanding."),
    ])
    assert scan_open_background_tasks(path) == []                              # re-arm scan
    assert scan_open_background_tasks(path, treat_killed_as_open=False) == []  # is_parked scan
    assert is_parked(path) is False


def test_task_stop_of_some_other_task_closes_nothing(tmp_path):
    path = _write(tmp_path, [
        _assistant_tool("toolu_bg", "Bash", {"command": "sleep 300", "run_in_background": True}),
        _ack("toolu_bg", {"backgroundTaskId": "keep"}, ""),
        _assistant_tool("toolu_stop", "TaskStop", {"task_id": "other"}),
        _ack("toolu_stop", {"message": "Successfully stopped task: other", "task_id": "other"}, ""),
    ])
    assert [t.tool_use_id for t in scan_open_background_tasks(path)] == ["toolu_bg"]
    assert is_parked(path) is True


def test_open_task_carries_the_launch_timestamp(tmp_path):
    launch = _assistant_tool("tu1", "Bash", {"command": "sleep 99", "run_in_background": True,
                                             "description": "long sleep"})
    launch["timestamp"] = "2026-10-02T11:00:00.000Z"
    path = _write(tmp_path, [launch, _tool_result("tu1")])
    [t] = scan_open_background_tasks(path)
    assert t.started_at == "2026-10-02T11:00:00.000Z"
    assert OpenBgTask.from_dict(t.to_dict()).started_at == t.started_at
    # a pre-#2635 snapshot has no key → None, not a crash
    assert OpenBgTask.from_dict({"tool_use_id": "x", "kind": "task"}).started_at is None


def test_pending_wakeup_reports_the_fire_time(tmp_path):
    from server.sessions.background_tasks import pending_wakeup
    arm = _assistant_tool("tu1", "ScheduleWakeup", {"delaySeconds": 600, "prompt": "p", "reason": "r"})
    arm["timestamp"] = "2026-10-02T11:00:00.000Z"
    path = _write(tmp_path, [_user("go"), arm, _tool_result("tu1")])
    now = datetime(2026, 10, 2, 11, 5, tzinfo=timezone.utc)
    w = pending_wakeup(path, now=now)
    assert w is not None and w.fire_at == datetime(2026, 10, 2, 11, 10, tzinfo=timezone.utc)
    assert scan_pending_wakeup(path, now=now) is True
    # well past fire + grace → lost, not pending
    late = datetime(2026, 10, 2, 11, 20, tzinfo=timezone.utc)
    assert pending_wakeup(path, now=late) is None


def test_pending_wakeup_without_timestamp_is_armed_with_unknown_fire_time(tmp_path):
    from server.sessions.background_tasks import pending_wakeup
    arm = _assistant_tool("tu1", "ScheduleWakeup", {"delaySeconds": 600, "prompt": "p", "reason": "r"})
    path = _write(tmp_path, [_user("go"), arm, _tool_result("tu1")])
    w = pending_wakeup(path)
    assert w is not None and w.fire_at is None


def test_background_work_snapshot_shape(tmp_path):
    from server.sessions.background_tasks import (background_work_snapshot, PendingWakeup,
                                                  EMPTY_BACKGROUND_WORK)
    shell = OpenBgTask(tool_use_id="tu1", kind="task", name="Bash",
                       input={"command": "pytest -q", "run_in_background": True},
                       description="", started_at="2026-10-02T11:00:00.000Z")
    mon = OpenBgTask(tool_use_id="tu2", kind="monitor", name="Monitor",
                     input={"command": "gh pr checks 1", "description": "watch CI"},
                     description="watch CI")
    agent = OpenBgTask(tool_use_id="tu3", kind="agent", name="Agent", input={}, description="brief",
                       replayable=False)
    snap = background_work_snapshot([shell, mon, agent],
                                    PendingWakeup(fire_at=datetime(2026, 10, 2, 11, 10, tzinfo=timezone.utc)))
    assert snap == {
        "items": [
            {"toolUseId": "tu1", "kind": "task", "name": "Bash", "description": "pytest -q",
             "startedAt": "2026-10-02T11:00:00.000Z"},
            {"toolUseId": "tu2", "kind": "monitor", "name": "Monitor", "description": "watch CI",
             "startedAt": None},
        ],
        "wakeup": True,
        "wakeupAt": "2026-10-02T11:10:00+00:00",
    }
    assert background_work_snapshot([], None) == EMPTY_BACKGROUND_WORK
    assert background_work_snapshot([], PendingWakeup(fire_at=None)) == {
        "items": [], "wakeup": True, "wakeupAt": None}
