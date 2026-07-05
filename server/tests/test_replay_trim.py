import json
import asyncio
from datetime import datetime
from server.sessions.replay import replay_window
from server.sessions import events as ev


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _turn(uuid_u, uuid_a, text):
    return [
        {"type": "user", "uuid": uuid_u, "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": uuid_a,
         "message": {"role": "assistant", "model": "claude-x",
                     "content": [{"type": "text", "text": text + "-reply"}]}},
    ]


def _turn_ts(uuid_u, uuid_a, text, ts_u, ts_a):
    """A completed turn whose entries carry CLI-style ISO timestamps."""
    return [
        {"type": "user", "uuid": uuid_u, "timestamp": ts_u,
         "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": uuid_a, "timestamp": ts_a,
         "message": {"role": "assistant", "model": "claude-x",
                     "content": [{"type": "text", "text": text + "-reply"}]}},
    ]


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def test_drop_incomplete_preserves_completed_turn_when_disk_lags(tmp_path):
    """THE resume-lag amputation bug (B2): on a LIVE attach mid-turn, the CLI has not
    yet written the in-progress turn's user entry (it spends seconds booting its MCP
    fleet after a resume before touching the JSONL). The disk's positionally-last
    UserMessage then belongs to the PREVIOUS, COMPLETED turn — dropping it amputates
    history the live buffer/ring will never re-supply (no seq gap → no self-heal).
    With turn_started_at known and every disk user entry strictly OLDER, nothing may
    be dropped."""
    rows = _turn_ts("u1", "a1", "first", "2026-07-05T10:00:00Z", "2026-07-05T10:00:05Z")
    rows += _turn_ts("u2", "a2", "second", "2026-07-05T10:01:00Z", "2026-07-05T10:01:05Z")
    path = _write(tmp_path, rows)
    # Turn 3 started a full minute after turn 2's entries were written — its user
    # entry is NOT on disk yet.
    started = _epoch("2026-07-05T10:02:00Z")
    kept = asyncio.run(replay_window(
        path, drop_incomplete_turn=True, turn_started_at=started)).events
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first", "second"]
    assert any(getattr(e, "text", "") == "second-reply" for e in kept)


def test_drop_incomplete_drops_disk_entry_of_in_progress_turn(tmp_path):
    """Regression guard for the duplication bug the drop exists for: when the disk
    DOES already hold the in-progress turn's user entry (ts >= turn_started_at), it
    must still be dropped — the live buffer replays that turn, so keeping it here
    duplicates it."""
    rows = _turn_ts("u1", "a1", "first", "2026-07-05T10:00:00Z", "2026-07-05T10:00:05Z")
    rows += [{"type": "user", "uuid": "u2", "timestamp": "2026-07-05T10:02:01Z",
              "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2", "timestamp": "2026-07-05T10:02:02Z",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)
    started = _epoch("2026-07-05T10:02:00Z")
    kept = asyncio.run(replay_window(
        path, drop_incomplete_turn=True, turn_started_at=started)).events
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first"]
    assert not any(getattr(e, "text", "") == "partial" for e in kept)


def test_drop_incomplete_slack_covers_coarse_clocks(tmp_path):
    """A disk user entry stamped just BEFORE turn_started_at (sub-2s skew from coarse
    clock granularity) must still count as the in-progress turn and be dropped."""
    rows = _turn_ts("u1", "a1", "first", "2026-07-05T10:00:00Z", "2026-07-05T10:00:05Z")
    rows += [{"type": "user", "uuid": "u2", "timestamp": "2026-07-05T10:01:59Z",
              "message": {"role": "user", "content": "second"}}]
    path = _write(tmp_path, rows)
    started = _epoch("2026-07-05T10:02:00Z")   # entry is 1s older — within the 2s slack
    kept = asyncio.run(replay_window(
        path, drop_incomplete_turn=True, turn_started_at=started)).events
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first"]


def test_drop_incomplete_missing_timestamp_keeps_legacy_positional_drop(tmp_path):
    """Documented decision: a timestamp-less trailing user entry can't be proven to
    belong to an old turn, so a known turn_started_at does NOT rescue it — the legacy
    positional drop applies. Real CLI transcripts always stamp timestamps; changing
    behavior for timestamp-less lines isn't worth the risk."""
    rows = _turn("u1", "a1", "first")
    rows += [{"type": "user", "uuid": "u2", "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)
    kept = asyncio.run(replay_window(
        path, drop_incomplete_turn=True, turn_started_at=1751710000.0)).events
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first"]
    assert not any(getattr(e, "text", "") == "partial" for e in kept)


def test_drop_incomplete_turn_removes_trailing_user_onward(tmp_path):
    rows = _turn("u1", "a1", "first") + _turn("u2", "a2", "second")
    # trailing in-progress turn: real user + one completed assistant text block
    rows += [{"type": "user", "uuid": "u3", "message": {"role": "user", "content": "third"}},
             {"type": "assistant", "uuid": "a3",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)

    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    users = [e for e in kept if isinstance(e, ev.UserMessage)]
    assert [u.text for u in users] == ["first", "second"]     # "third" dropped
    assert not any(getattr(e, "text", None) == "partial" for e in kept)

    full = asyncio.run(replay_window(path, drop_incomplete_turn=False)).events
    assert [u.text for u in full if isinstance(u, ev.UserMessage)] == ["first", "second", "third"]


def test_compact_summary_is_not_a_user_turn(tmp_path):
    """The CLI's auto-compaction summary is a ``type:"user"`` entry flagged
    ``isCompactSummary`` (NOT ``isMeta``). It is harness plumbing, not a real user
    prompt, so it must not render as a user bubble — otherwise a reloaded transcript
    shows a giant "This session is being continued…" message the live view never had."""
    rows = _turn("u1", "a1", "first")
    rows += [{"type": "user", "uuid": "cs", "isCompactSummary": True,
              "isVisibleInTranscriptOnly": True,
              "message": {"role": "user",
                          "content": "This session is being continued from a previous conversation…"}}]
    rows += _turn("u2", "a2", "second")
    path = _write(tmp_path, rows)
    full = asyncio.run(replay_window(path, drop_incomplete_turn=False)).events
    assert [u.text for u in full if isinstance(u, ev.UserMessage)] == ["first", "second"]


def test_drop_incomplete_turn_boundary_ignores_compact_summary(tmp_path):
    """Regression: a mid-session auto-compaction writes a ``isCompactSummary`` user
    entry. ``drop_incomplete_turn`` must anchor on the last REAL user prompt, not on
    the compaction summary — otherwise every completed post-compaction turn (which
    lives only on disk, not in the live in-progress buffer) is dropped on reattach and
    vanishes from the reloaded transcript."""
    rows = _turn("u1", "a1", "first")
    # auto-compaction boundary, then a COMPLETED post-compaction turn …
    rows += [{"type": "user", "uuid": "cs", "isCompactSummary": True,
              "isVisibleInTranscriptOnly": True,
              "message": {"role": "user", "content": "This session is being continued…"}}]
    rows += _turn("u2", "a2", "second")
    # … then the trailing in-progress turn owned by the live buffer.
    rows += [{"type": "user", "uuid": "u3", "message": {"role": "user", "content": "third"}},
             {"type": "assistant", "uuid": "a3",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)
    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    # Only "third" (the real in-progress turn) is dropped; the completed post-compaction
    # "second" turn is preserved.
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first", "second"]
    assert any(getattr(e, "text", "") == "second-reply" for e in kept)
    assert not any(getattr(e, "text", "") == "partial" for e in kept)


def test_drop_incomplete_turn_ignores_tool_result_user_lines(tmp_path):
    rows = _turn("u1", "a1", "first")
    # in-progress turn: real user, assistant tool_use, then a tool_result "user" line
    rows += [{"type": "user", "uuid": "u2", "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "tool_use", "id": "tool1", "name": "Bash", "input": {}}]}},
             {"type": "user", "uuid": "ur",
              "message": {"role": "user",
                          "content": [{"type": "tool_result", "tool_use_id": "tool1", "content": "ok"}]}}]
    path = _write(tmp_path, rows)
    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    # The trim boundary is the last REAL user ("second"), not the tool_result line.
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first"]
