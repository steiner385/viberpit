import json
from server.sessions.background_tasks import (
    scan_open_background_tasks, build_rearm_prompt, OpenBgTask,
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


def test_build_rearm_prompt_guards_only_non_monitor():
    mon = OpenBgTask("toolu_mon", "monitor", "Monitor", {"command": "z"}, "Watch PR")
    bash = OpenBgTask("toolu_bg", "task", "Bash", {"command": "x", "run_in_background": True}, "Poll")
    guard = "interrupted by a server restart"
    assert guard not in build_rearm_prompt([mon])
    p = build_rearm_prompt([mon, bash])
    assert guard in p
    assert '"command": "z"' in p and '"command": "x"' in p   # verbatim input JSON
