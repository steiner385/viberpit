"""On attach, a session whose question died with the daemon re-offers it.

Without this the replayed card stays live:false forever — PendingQuestionPanel
selects on `live`, so the only rendering is the read-only inline copy and the user
has no way to answer.
"""
import asyncio
import json

from server.bridge.ws import _orphaned_ask_frames


def _write(tmp_path, tool_id, header):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "assistant", "uuid": "u1", "message": {
        "role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {
                "questions": [{"header": header, "question": "Q?", "multiSelect": False,
                               "options": [{"label": "Close it"}]}]}}]}}))
    return str(p)


class _Orch:
    def __init__(self, pending, kind, path, perms=()):
        self._pending, self._kind, self._path = pending, kind, path
        self._perms = list(perms)

    async def pending_questions(self, sid):
        return self._pending

    async def pending_permissions(self, sid):
        return self._perms

    def orphaned_interaction(self, sid):
        return self._kind

    def transcript_path(self, sid):
        return self._path


def test_reoffers_the_transcript_question_when_nothing_is_live(tmp_path):
    path = _write(tmp_path, "toolu_A", "Disposition")
    frames = asyncio.run(_orphaned_ask_frames(_Orch([], "question", path), "s1"))
    assert len(frames) == 1
    assert frames[0]["type"] == "question"
    assert frames[0]["orphaned"] is True
    assert frames[0]["questionId"] == "toolu_A-0"
    assert frames[0]["header"] == "Disposition"


def test_no_reoffer_when_a_live_question_is_pending(tmp_path):
    """A live pending question is already re-emitted by the existing path; adding a
    second, orphaned copy of the same card would double it."""
    path = _write(tmp_path, "toolu_A", "Disposition")
    live = [{"question_id": "live-1", "turn_id": "t", "header": "H", "question": "Q",
             "options": [], "multi_select": False}]
    assert asyncio.run(_orphaned_ask_frames(_Orch(live, "question", path), "s1")) == []


def test_no_reoffer_for_a_healthy_session(tmp_path):
    path = _write(tmp_path, "toolu_A", "Disposition")
    assert asyncio.run(_orphaned_ask_frames(_Orch([], None, path), "s1")) == []


def test_an_orphaned_permission_never_reoffers_a_question(tmp_path):
    """The kinds stay apart: a permission orphan rebuilds a permission card from its
    gated tool_use, never the transcript's last question."""
    path = _write(tmp_path, "toolu_A", "Disposition")
    assert asyncio.run(_orphaned_ask_frames(_Orch([], "permission", path), "s1")) == []


def test_no_transcript_is_not_an_error():
    assert asyncio.run(_orphaned_ask_frames(_Orch([], "question", None), "s1")) == []


# ── #2922: permission and plan orphans are re-offered too ────────────────────

def _write_use(tmp_path, tool_id, name, inp):
    from server.sessions.orphaned_asks import RESTART_ASK_MESSAGE
    p = tmp_path / "p.jsonl"
    rows = [
        {"type": "assistant", "uuid": "u9", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}},
        {"type": "user", "uuid": "r9", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
             "content": RESTART_ASK_MESSAGE}]}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def test_reoffers_an_orphaned_permission(tmp_path):
    from server.sessions.orphaned_asks import ORPHAN_PERM_PREFIX
    path = _write_use(tmp_path, "toolu_B", "Bash", {"command": "make"})
    frames = asyncio.run(_orphaned_ask_frames(_Orch([], "permission", path), "s1"))
    assert len(frames) == 1
    f = frames[0]
    assert f["type"] == "permissionRequest"
    assert f["permId"] == f"{ORPHAN_PERM_PREFIX}toolu_B"
    assert f["tool"] == "Bash" and f["plan"] is False
    assert f["input"] == {"command": "make"}


def test_reoffers_an_orphaned_plan(tmp_path):
    path = _write_use(tmp_path, "toolu_P", "ExitPlanMode", {"plan": "x"})
    frames = asyncio.run(_orphaned_ask_frames(_Orch([], "plan", path), "s1"))
    assert len(frames) == 1 and frames[0]["plan"] is True


def test_no_permission_reoffer_while_a_live_permission_is_pending(tmp_path):
    path = _write_use(tmp_path, "toolu_B", "Bash", {"command": "make"})
    live = [{"permId": "p1", "tool": "Bash", "input": {}, "turn_id": "t"}]
    assert asyncio.run(_orphaned_ask_frames(
        _Orch([], "permission", path, perms=live), "s1")) == []
