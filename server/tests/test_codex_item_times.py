"""The Codex item-time sidecar: the journal the driver appends and replay reads.

`thread/read` carries no per-item time, so these files are the ONLY source of
per-block timestamps for a reloaded Codex session. The contract under test:
append-only, tolerant of every kind of damage on read, never able to fail a
session on write, and safe against a thread id that is not a plain token.
"""
from __future__ import annotations

import json
from pathlib import Path

from server.codex.item_times import (
    EMPTY_ITEM_TIMES,
    ItemTimesWriter,
    item_times_path,
    load_item_times,
    uuid7_ms,
)


def test_path_is_the_thread_id_under_the_root(tmp_path: Path) -> None:
    assert item_times_path(tmp_path, "01a0a0d6-b4da-7270-9b87-6301c87e0ca6") == \
        tmp_path / "01a0a0d6-b4da-7270-9b87-6301c87e0ca6.jsonl"


def test_a_thread_id_that_is_not_a_plain_token_gets_no_path(tmp_path: Path) -> None:
    for bad in ("", "../etc/passwd", "a/b", "a.b", ".hidden", "x" * 129):
        assert item_times_path(tmp_path, bad) is None, bad


def test_writer_then_loader_round_trip(tmp_path: Path) -> None:
    w = ItemTimesWriter(tmp_path / "codex-item-times")
    w.record_started("t1", "exec-1", 1_000)
    w.record_completed("t1", "exec-1", 1_750)
    w.record_started("t1", "msg_1", 2_000)
    times = load_item_times(item_times_path(w.root, "t1"))
    assert dict(times.started) == {"exec-1": 1_000, "msg_1": 2_000}
    assert dict(times.completed) == {"exec-1": 1_750}


def test_each_thread_has_its_own_file(tmp_path: Path) -> None:
    w = ItemTimesWriter(tmp_path)
    w.record_started("t1", "a", 1)
    w.record_started("t2", "b", 2)
    assert dict(load_item_times(item_times_path(tmp_path, "t1")).started) == {"a": 1}
    assert dict(load_item_times(item_times_path(tmp_path, "t2")).started) == {"b": 2}


def test_a_missing_file_and_a_none_path_load_as_empty(tmp_path: Path) -> None:
    assert load_item_times(tmp_path / "nope.jsonl") == EMPTY_ITEM_TIMES
    assert load_item_times(None) == EMPTY_ITEM_TIMES


def test_loader_skips_torn_and_foreign_lines_but_keeps_the_rest(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"i": "ok", "s": 5}) + "\n"
        + "\n"
        + "{not json\n"
        + json.dumps(["not", "a", "dict"]) + "\n"
        + json.dumps({"i": 7, "s": 1}) + "\n"           # id not a string
        + json.dumps({"i": "bool", "s": True}) + "\n"   # bool is not a time
        + json.dumps({"i": "str", "c": "9"}) + "\n"     # string is not a time
        + '{"i": "torn", "s": 12'                        # crash mid-write, no newline
    )
    times = load_item_times(p)
    assert dict(times.started) == {"ok": 5}
    assert dict(times.completed) == {}


def test_a_later_record_for_the_same_item_wins(tmp_path: Path) -> None:
    w = ItemTimesWriter(tmp_path)
    w.record_started("t1", "a", 1)
    w.record_started("t1", "a", 2)
    assert dict(load_item_times(item_times_path(tmp_path, "t1")).started) == {"a": 2}


def test_writer_never_raises_and_gives_up_on_a_thread_after_one_failure(
    tmp_path: Path, caplog,
) -> None:
    blocker = tmp_path / "root"
    blocker.write_text("a file where the directory should be")
    w = ItemTimesWriter(blocker)
    with caplog.at_level("WARNING", logger="server.codex.item_times"):
        w.record_started("t1", "a", 1)
        w.record_started("t1", "b", 2)
    assert sum("journal failed" in r.message for r in caplog.records) == 1


def test_writer_refuses_an_unsafe_thread_id_without_raising(tmp_path: Path) -> None:
    w = ItemTimesWriter(tmp_path)
    w.record_started("../escape", "a", 1)
    assert list(tmp_path.iterdir()) == []


def test_uuid7_ms_decodes_the_embedded_millisecond() -> None:
    # A real userMessage id from codex-cli 0.153.2; its turn's startedAt was
    # 1789404900 s, and the id decodes to 1.409 s after that.
    assert uuid7_ms("01a0a0d7-f021-7343-83aa-383fc474d189") == 1_789_404_901_409.0


def test_uuid7_ms_is_none_for_every_other_id_shape() -> None:
    for other in ("msg_0f14725e47165b83016aa826e744ec87d19d",
                  "exec-0f236a54-66ab-4257-89e3-8eb95d51c3d1",
                  "0f236a54-66ab-4257-89e3-8eb95d51c3d1",   # a v4
                  "", "not-a-uuid"):
        assert uuid7_ms(other) is None, other
