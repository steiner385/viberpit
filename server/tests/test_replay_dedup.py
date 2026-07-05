"""Replay suppression of the duplicate user turn a recovery *resend* leaves behind.

Rate-limit / crash recovery resends the last prompt into a ``resume=``d driver
(orchestrator ``_replay_same_account`` / ``_reassign``). The ORIGINAL user turn was
already persisted to the CLI's transcript JSONL before the failure, so the resend
appends a SECOND identical UserMessage to the same file — the CLI owns the JSONL
(constitution I), so we dedupe on replay reconstruction, not at write time.

Distinct mechanism from ``drop_incomplete_turn`` (see test_replay_trim.py): this runs
on settled history anywhere in the file; that one trims the in-progress tail.
"""
import json
import asyncio
from server.sessions.replay import replay_window
from server.sessions import events as ev


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _user(uuid, text):
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def _user_with_image(uuid, text, data):
    # Mirrors the CLI's on-disk image-block shape (see test_replay_images.py).
    content = [{"type": "text", "text": text}] if text else []
    content.append({"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/png", "data": data}})
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": content}}


def _assistant(uuid, text):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "model": "claude-x",
                        "content": [{"type": "text", "text": text}]}}


def _events(path):
    return asyncio.run(replay_window(path, limit=None)).events


def _user_texts(events):
    return [e.text for e in events if isinstance(e, ev.UserMessage)]


def _assistant_texts(events):
    return [e.text for e in events if isinstance(e, ev.TextDelta)]


def test_duplicate_user_then_assistant_collapses_to_one(tmp_path):
    """[user X][user X][assistant done] → one user turn, one assistant turn.

    The first (dead) resend head is suppressed; the second carries the completion."""
    rows = [_user("u1", "X"), _user("u2", "X"), _assistant("a1", "done")]
    events = _events(_write(tmp_path, rows))
    assert _user_texts(events) == ["X"]
    assert _assistant_texts(events) == ["done"]
    # The surviving user turn is the LATER one (it precedes the real completion).
    assert [e.turn_id for e in events if isinstance(e, ev.UserMessage)] == ["u2"]


def test_interrupted_turn_with_partial_output_keeps_both(tmp_path):
    """[user X][assistant partial][user X][assistant done] → BOTH user turns kept.

    The earlier turn produced real assistant content (a partial completion that is
    genuine transcript), so it must never be suppressed."""
    rows = [_user("u1", "X"), _assistant("a1", "partial"),
            _user("u2", "X"), _assistant("a2", "done")]
    events = _events(_write(tmp_path, rows))
    assert _user_texts(events) == ["X", "X"]
    assert _assistant_texts(events) == ["partial", "done"]


def test_different_texts_kept(tmp_path):
    """[user X][user Y] different texts → both kept (not a resend duplicate)."""
    rows = [_user("u1", "X"), _user("u2", "Y"), _assistant("a1", "done")]
    events = _events(_write(tmp_path, rows))
    assert _user_texts(events) == ["X", "Y"]


def test_same_text_different_images_both_kept(tmp_path):
    """[user "" +imgA][user "" +imgB] same (empty) text, DIFFERENT images → both kept.

    A true recovery resend re-persists the identical prompt, images included. Two
    adjacent turns that merely share caption text (here both blank) but carry
    different images are NOT a resend duplicate — collapsing them would silently
    drop the first image."""
    rows = [_user_with_image("u1", "", "QUJD"), _user_with_image("u2", "", "WFla"),
            _assistant("a1", "done")]
    events = _events(_write(tmp_path, rows))
    user_events = [e for e in events if isinstance(e, ev.UserMessage)]
    assert [e.turn_id for e in user_events] == ["u1", "u2"]
    assert user_events[0].images[0]["url"] == "data:image/png;base64,QUJD"
    assert user_events[1].images[0]["url"] == "data:image/png;base64,WFla"


def test_same_text_same_images_collapses(tmp_path):
    """[user X +imgA][user X +imgA] identical text AND images → the dead head still
    collapses (the recovery-resend shape is unaffected by the image-equality check)."""
    rows = [_user_with_image("u1", "X", "QUJD"), _user_with_image("u2", "X", "QUJD"),
            _assistant("a1", "done")]
    events = _events(_write(tmp_path, rows))
    user_events = [e for e in events if isinstance(e, ev.UserMessage)]
    assert [e.turn_id for e in user_events] == ["u2"]
    assert user_events[0].images[0]["url"] == "data:image/png;base64,QUJD"


def test_n_consecutive_resends_collapse_to_one(tmp_path):
    """[user X][user X][user X][assistant done] → collapses to a single user turn."""
    rows = [_user("u1", "X"), _user("u2", "X"), _user("u3", "X"),
            _assistant("a1", "done")]
    events = _events(_write(tmp_path, rows))
    assert _user_texts(events) == ["X"]
    assert [e.turn_id for e in events if isinstance(e, ev.UserMessage)] == ["u3"]
    assert _assistant_texts(events) == ["done"]


def test_meta_line_between_duplicates_does_not_defeat_suppression(tmp_path):
    """An ``isMeta`` (or system/summary) entry between the two identical user messages
    emits no event, so the two UserMessages stay adjacent and suppression still fires."""
    rows = [_user("u1", "X"),
            {"type": "user", "uuid": "m1", "isMeta": True,
             "message": {"role": "user", "content": "Base directory for this skill: …"}},
            _user("u2", "X"),
            _assistant("a1", "done")]
    events = _events(_write(tmp_path, rows))
    assert _user_texts(events) == ["X"]
    assert [e.turn_id for e in events if isinstance(e, ev.UserMessage)] == ["u2"]


def test_windowing_correct_after_suppression(tmp_path):
    """Paging/turn counts stay correct after a mid-history resend is collapsed: the
    dead head carries no assistant TurnStart, so total_turns is unchanged, and the
    surviving turns page as usual."""
    # turn 0 (real), a resend-duplicated turn 1, then turns 2..4.
    rows = [_user("u0", "q0"), _assistant("a0", "r0")]
    rows += [_user("u1a", "q1"), _user("u1b", "q1"), _assistant("a1", "r1")]
    rows += [_user("u2", "q2"), _assistant("a2", "r2")]
    rows += [_user("u3", "q3"), _assistant("a3", "r3")]
    rows += [_user("u4", "q4"), _assistant("a4", "r4")]
    path = _write(tmp_path, rows)

    full = asyncio.run(replay_window(path, limit=None))
    assert full.total_turns == 5
    # The duplicate "q1" head is gone; each turn's user prompt appears once.
    assert _user_texts(full.events) == ["q0", "q1", "q2", "q3", "q4"]

    # Last-3 window: turns 2..4, earliest_turn == 2.
    win = asyncio.run(replay_window(path, limit=3))
    assert win.total_turns == 5
    assert win.earliest_turn == 2
    assert [e.turn_id for e in win.events if isinstance(e, ev.TurnStart)] == ["a2", "a3", "a4"]
    assert _user_texts(win.events) == ["q2", "q3", "q4"]
