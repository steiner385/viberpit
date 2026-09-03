"""Re-offering an orphaned question needs the SAME question ids replay assigned,
or the client's reducer creates a second, duplicate card instead of upgrading the
one already on screen."""
import json

from server.sessions import events as ev
from server.sessions.replay import last_question_events


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _ask(tool_id, header, options, timestamp="2026-09-03T12:00:00.000Z"):
    return {"type": "assistant", "uuid": f"u-{tool_id}", "timestamp": timestamp,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion",
                 "input": {"questions": [
                     {"header": header, "question": f"{header}?", "multiSelect": False,
                      "options": [{"label": o} for o in options]}]}}]}}


def test_question_event_carries_orphaned_on_the_wire():
    w = ev.Question(question_id="q1", turn_id="t1", header="H", question="Q?",
                    options=[], multi_select=False, orphaned=True).to_wire()
    assert w["orphaned"] is True


def test_question_event_defaults_to_not_orphaned():
    """The live path must be untouched — every existing emitter omits the flag."""
    w = ev.Question(question_id="q1", turn_id="t1", header="H", question="Q?",
                    options=[], multi_select=False).to_wire()
    assert w["orphaned"] is False


def test_last_question_events_uses_replay_derived_ids(tmp_path):
    path = _write(tmp_path, [_ask("toolu_A", "Disposition", ["Close it", "Keep it"])])
    out = last_question_events(path)
    assert [q.question_id for q in out] == ["toolu_A-0"]
    assert out[0].header == "Disposition"
    assert [o["label"] for o in out[0].options] == ["Close it", "Keep it"]


def test_last_question_events_returns_only_the_most_recent_call(tmp_path):
    """A long session may have asked several times; only the one open at the
    restart is re-offered."""
    path = _write(tmp_path, [_ask("toolu_A", "First", ["a"]), _ask("toolu_B", "Second", ["b"])])
    out = last_question_events(path)
    assert [q.question_id for q in out] == ["toolu_B-0"]
    assert out[0].header == "Second"


def test_last_question_events_marks_them_orphaned(tmp_path):
    path = _write(tmp_path, [_ask("toolu_A", "Disposition", ["Close it"])])
    assert all(q.orphaned for q in last_question_events(path))


def test_last_question_events_is_empty_without_a_question(tmp_path):
    path = _write(tmp_path, [{"type": "assistant", "uuid": "u1", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}]}}])
    assert last_question_events(path) == []


def test_last_question_events_never_raises_on_a_missing_file(tmp_path):
    assert last_question_events(str(tmp_path / "nope.jsonl")) == []


def test_last_question_events_ts_is_numeric_millis(tmp_path):
    """Question.ts is float-millis on the wire (and numeric in the client's
    EvQuestion). Passing the raw ISO `timestamp` string through would mint a
    block with a string ts whenever the question fell outside the replay window
    — i.e. exactly the case the re-offer exists for."""
    path = _write(tmp_path, [_ask("toolu_A", "Disposition", ["Close it"])])
    out = last_question_events(path)
    assert out and isinstance(out[0].ts, float)


def test_last_question_events_ts_is_none_when_the_timestamp_is_unusable(tmp_path):
    """No timestamp / a garbage one degrades to None, never to a string."""
    for bad in (None, "not-a-date"):
        row = _ask("toolu_A", "Disposition", ["Close it"], timestamp=bad)
        if bad is None:
            row.pop("timestamp")
        out = last_question_events(_write(tmp_path, [row]))
        assert out and out[0].ts is None
