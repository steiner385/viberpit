"""Re-offering a permission / plan approval a daemon restart orphaned (#2922).

Only questions used to be re-offered. A permission card leaves no record of its
own in the transcript, but the tool_use it gated does — and the restart close
writes ``RESTART_ASK_MESSAGE`` as that tool_use's result — so the card can be
rebuilt from it.
"""
import json

from server.sessions.orphaned_asks import ORPHAN_PERM_PREFIX, RESTART_ASK_MESSAGE
from server.sessions.replay import last_orphaned_permission
from server.sessions.transcript_repair import INTERRUPT_TEXT, RESTART_INTERRUPT_TEXT


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _use(uuid, tool_id, name, inp, *, side=False):
    return {"type": "assistant", "uuid": uuid, "isSidechain": side,
            "timestamp": "2026-10-11T12:00:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def _result(tool_id, content, is_error=True):
    return {"type": "user", "uuid": f"r-{tool_id}", "toolUseResult": "Error",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content,
                 "is_error": is_error}]}}


def test_rebuilds_the_permission_the_restart_closed(tmp_path):
    path = _write(tmp_path, [
        _use("u1", "toolu_old", "Bash", {"command": "ls"}),
        _result("toolu_old", "a b c", is_error=False),
        _use("u2", "toolu_make", "Bash", {"command": "make deploy-check"}),
        _result("toolu_make", RESTART_ASK_MESSAGE),
    ])
    req = last_orphaned_permission(path, plan=False)
    assert req is not None
    assert req.perm_id == f"{ORPHAN_PERM_PREFIX}toolu_make"
    assert req.tool == "Bash"
    assert req.input == {"command": "make deploy-check"}
    assert req.turn_id == "u2"
    assert req.plan is False
    assert req.always_allowed is False   # no waiter left to hold a grant against


def test_falls_back_to_the_trailing_interrupted_tool_use(tmp_path):
    for text in (INTERRUPT_TEXT, RESTART_INTERRUPT_TEXT, None):
        rows = [_use("u2", "toolu_w", "Write", {"file_path": "/x"})]
        if text is not None:
            rows.append(_result("toolu_w", text))
        req = last_orphaned_permission(_write(tmp_path, rows), plan=False)
        assert req is not None and req.tool == "Write", text


def test_plan_kind_only_matches_exit_plan_mode(tmp_path):
    rows = [_use("u1", "toolu_p", "ExitPlanMode", {"plan": "do it"}),
            _result("toolu_p", RESTART_ASK_MESSAGE)]
    path = _write(tmp_path, rows)
    assert last_orphaned_permission(path, plan=False) is None
    req = last_orphaned_permission(path, plan=True)
    assert req is not None and req.plan is True and req.tool == "ExitPlanMode"


def test_a_question_is_never_a_permission(tmp_path):
    rows = [_use("u1", "toolu_q", "AskUserQuestion", {"questions": []}),
            _result("toolu_q", RESTART_ASK_MESSAGE)]
    assert last_orphaned_permission(_write(tmp_path, rows), plan=False) is None


def test_an_answered_tail_is_not_orphaned(tmp_path):
    rows = [_use("u1", "toolu_ok", "Bash", {"command": "ls"}),
            _result("toolu_ok", "fine", is_error=False)]
    assert last_orphaned_permission(_write(tmp_path, rows), plan=False) is None


def test_sidechain_tool_uses_are_ignored(tmp_path):
    rows = [_use("u1", "toolu_s", "Bash", {"command": "ls"}, side=True)]
    assert last_orphaned_permission(_write(tmp_path, rows), plan=False) is None


def test_missing_file_is_none(tmp_path):
    assert last_orphaned_permission(str(tmp_path / "nope.jsonl"), plan=False) is None
