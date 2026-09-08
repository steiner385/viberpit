"""Codex background-work parity (#1284): observe it live, never replay it blindly.

Three layers, tested where each one can actually be wrong:

* the pinned `thread/backgroundTerminals/list` contract and its DTOs;
* :mod:`server.codex.background` — pagination, de-duplication, and the
  ``replayable=False`` normalization that encodes the vendor boundary;
* the orchestrator seams that make a Codex session park, un-park across a daemon
  restart, and get *told* about lost terminals instead of re-running them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.codex import background, schema
from server.codex.protocol import CodexAppServerClient
from server.sessions.background_tasks import OpenBgTask, build_rearm_prompt

FIXTURES = (
    Path(__file__).parent / "fixtures" / "codex_app_server" / "0.153.2"
)


def _terminal(process_id: str, command: str = "npm run dev", cwd: str = "/srv/app"):
    return {
        "processId": process_id,
        "itemId": f"item-{process_id}",
        "command": command,
        "cwd": cwd,
    }


class _FakeLister:
    """An app-server that answers `thread/backgroundTerminals/list` from pages."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.calls: list[schema.ThreadBackgroundTerminalsListParams] = []

    async def list_background_terminals(self, params):
        self.calls.append(params)
        index = 0 if params.cursor is None else int(params.cursor)
        page = self._pages[index]
        return schema.parse_thread_background_terminals_list_response(page)


# ---------------------------------------------------------------------------
# The pinned contract
# ---------------------------------------------------------------------------


def test_the_list_method_is_pinned_and_supported():
    """Verified against the real binary, not assumed: the fixture IS the generated
    document, and the method must be in the supported set to leave the client gate."""
    assert (
        schema.METHOD_THREAD_BACKGROUND_TERMINALS_LIST
        in schema.SUPPORTED_CLIENT_REQUESTS
    )
    for name in (
        "v2/ThreadBackgroundTerminalsListParams.json",
        "v2/ThreadBackgroundTerminalsListResponse.json",
    ):
        assert name in schema.CURATED_SCHEMA_FILES
        assert (FIXTURES / name.replace("/", "__")).is_file()


def test_terminate_and_clean_stay_unpinned():
    """ccswitch never ends a background terminal itself — daemon shutdown and the boot
    sweep already do, by process tree. Pinning a method with no caller is dead surface."""
    curated = " ".join(schema.CURATED_SCHEMA_FILES)
    assert "BackgroundTerminalsTerminate" not in curated
    assert "BackgroundTerminalsClean" not in curated


def test_the_vendor_declares_no_way_to_start_one():
    """The load-bearing fact behind ``replayable=False``.

    The app-server's whole background-terminal surface is list/terminate/clean —
    three ways to observe or end one, none to start one. Read off the generated
    envelope inventory rather than asserted from memory, so a future release that
    adds a relaunch primitive fails this test instead of silently leaving ccswitch
    reporting a limitation that no longer exists.
    """
    index = json.loads((FIXTURES / "method_index.json").read_text())
    methods = {
        m for m in index["clientRequests"] if "backgroundTerminals" in m
    }
    assert methods == {
        "thread/backgroundTerminals/list",
        "thread/backgroundTerminals/terminate",
        "thread/backgroundTerminals/clean",
    }


def test_params_round_trip_through_the_outbound_validator():
    wire = schema.validate_client_request_params(
        schema.METHOD_THREAD_BACKGROUND_TERMINALS_LIST,
        {"threadId": "th-1", "limit": 50},
    )
    assert wire == {"threadId": "th-1", "limit": 50}


def test_params_reject_an_empty_thread_id():
    with pytest.raises(schema.CodexSchemaError):
        schema.ThreadBackgroundTerminalsListParams(thread_id="")


def test_response_parses_optional_telemetry():
    parsed = schema.parse_thread_background_terminals_list_response(
        {
            "data": [
                {**_terminal("p1"), "osPid": 4242, "cpuPercent": 1.5, "rssKb": 900},
                # A whole-valued double arrives as a JSON integer.
                {**_terminal("p2"), "cpuPercent": 2},
            ],
            "nextCursor": None,
        }
    )
    assert [t.process_id for t in parsed.data] == ["p1", "p2"]
    assert parsed.data[0].os_pid == 4242
    assert parsed.data[0].cpu_percent == 1.5
    assert parsed.data[1].cpu_percent == 2.0
    assert parsed.next_cursor is None


def test_an_entry_with_no_identity_is_dropped_not_fatal():
    """One unusable entry must not read as "this session has no background work" —
    that would un-park a session that really is waiting on something."""
    parsed = schema.parse_thread_background_terminals_list_response(
        {"data": [{"command": "x", "cwd": "/tmp"}, _terminal("p1")]}
    )
    assert [t.process_id for t in parsed.data] == ["p1"]


def test_a_malformed_response_raises():
    with pytest.raises(schema.CodexSchemaError):
        schema.parse_thread_background_terminals_list_response({"data": "nope"})


def test_the_client_routes_the_method_through_the_shared_call_path():
    """The typed convenience exists and the response decoder knows the method — a
    method in ``SUPPORTED_CLIENT_REQUESTS`` whose reply nothing can parse leaves
    ``call()`` raising on a perfectly good answer."""
    assert callable(CodexAppServerClient.list_background_terminals)
    decoded = schema.parse_client_response(
        schema.METHOD_THREAD_BACKGROUND_TERMINALS_LIST, {"data": [_terminal("p1")]}
    )
    assert isinstance(decoded, schema.ThreadBackgroundTerminalsListResponse)


# ---------------------------------------------------------------------------
# server/codex/background.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_follows_the_cursor_to_completion():
    lister = _FakeLister([
        {"data": [_terminal("p1")], "nextCursor": "1"},
        {"data": [_terminal("p2")], "nextCursor": None},
    ])
    found = await background.open_background_terminals(lister, "th-1")
    assert [t.process_id for t in found] == ["p1", "p2"]
    assert [c.thread_id for c in lister.calls] == ["th-1", "th-1"]
    assert lister.calls[0].limit == background.TERMINAL_PAGE_LIMIT


@pytest.mark.asyncio
async def test_pagination_stops_at_the_page_cap():
    """A peer that always hands back a cursor must not pin a turn-end status write."""
    lister = _FakeLister([{"data": [_terminal("p1")], "nextCursor": "0"}])
    found = await background.open_background_terminals(lister, "th-1", max_pages=3)
    assert len(lister.calls) == 3
    assert [t.process_id for t in found] == ["p1"]  # de-duplicated across pages


@pytest.mark.asyncio
async def test_a_terminal_seen_twice_is_counted_once():
    """A terminal exiting mid-read shifts the page window; double-counting it would
    overstate a session's background work."""
    lister = _FakeLister([
        {"data": [_terminal("p1"), _terminal("p2")], "nextCursor": "1"},
        {"data": [_terminal("p2"), _terminal("p3")], "nextCursor": None},
    ])
    found = await background.open_background_terminals(lister, "th-1")
    assert [t.process_id for t in found] == ["p1", "p2", "p3"]


def test_normalization_refuses_replay():
    task = background.to_open_task(
        schema.ThreadBackgroundTerminal(
            command="npm run dev", cwd="/srv/app", item_id="i1", process_id="p1"
        )
    )
    assert task.replayable is False
    assert task.kind == background.TERMINAL_KIND
    # The item id ties the report to the commandExecution item already in the
    # transcript, so a report names what the user is looking at.
    assert task.tool_use_id == "i1"
    assert task.input == {"command": "npm run dev", "cwd": "/srv/app"}


def test_normalization_survives_a_snapshot_round_trip():
    """``pending_rearm`` is persisted JSON — ``replayable`` has to come back."""
    task = background.to_open_task(
        schema.ThreadBackgroundTerminal(
            command="tail -f log", cwd="/srv", item_id="i1", process_id="p1"
        )
    )
    assert OpenBgTask.from_dict(json.loads(json.dumps(task.to_dict()))) == task


def test_a_legacy_snapshot_without_the_flag_stays_replayable():
    """Rows written by a pre-#1284 daemon could only hold Claude tasks."""
    legacy = {"tool_use_id": "toolu_1", "kind": "monitor", "name": "Monitor",
              "input": {"until": "x"}, "description": "watch"}
    assert OpenBgTask.from_dict(legacy).replayable is True


# ---------------------------------------------------------------------------
# The restart turn
# ---------------------------------------------------------------------------


def _codex_task(command="npm run deploy:prod", cwd="/srv/app"):
    return OpenBgTask(tool_use_id="i1", kind="terminal", name="BackgroundTerminal",
                      input={"command": command, "cwd": cwd},
                      description=command, replayable=False)


def test_a_lost_terminal_is_reported_never_relaunched():
    prompt = build_rearm_prompt([_codex_task()])
    assert "npm run deploy:prod" in prompt          # named, so the agent can judge it
    assert "/srv/app" in prompt
    assert "were NOT restarted" in prompt
    # The instruction half of the prompt must not appear for a non-replayable entry:
    # that sentence is what would make an agent re-run the command line.
    assert "Relaunch each one" not in prompt
    assert "relaunch the" not in prompt


def test_claude_tasks_still_get_the_relaunch_instruction():
    """Preserve working Claude behaviour — the parity work must not soften it."""
    task = OpenBgTask(tool_use_id="toolu_1", kind="monitor", name="Monitor",
                      input={"until": "done"}, description="watch CI")
    prompt = build_rearm_prompt([task])
    assert "Relaunch each one with exactly the parameters shown." in prompt
    assert 'relaunch the Monitor tool with input {"until": "done"}' in prompt
    assert "were NOT restarted" not in prompt


def test_a_mixed_snapshot_keeps_the_two_halves_apart():
    """One prompt, two sections: instructions for what may be replayed, a report for
    what may not. A mixed snapshot is possible for a fleet whose engines differ."""
    claude = OpenBgTask(tool_use_id="toolu_1", kind="monitor", name="Monitor",
                        input={"until": "done"}, description="watch CI")
    prompt = build_rearm_prompt([claude, _codex_task()])
    relaunch_at = prompt.index("Relaunch each one")
    report_at = prompt.index("were NOT restarted")
    assert relaunch_at < report_at
    # The command line lives strictly in the report half.
    assert prompt.index("npm run deploy:prod") > report_at


def test_an_empty_snapshot_builds_an_empty_prompt():
    assert build_rearm_prompt([]) == ""


# ---------------------------------------------------------------------------
# The orchestrator seams
# ---------------------------------------------------------------------------
#
# These run the REAL `_persist_status` / `rehydrate` / `_snapshot_open_tasks`
# against a real SQLite row, because the bug this parity closes is exactly a
# per-engine dispatch that a driver-level test cannot see.

import asyncio
import types

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions import events as ev
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _codex_orch(tmp_path, *, status="idle", **row):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}")
    base = dict(id="s1", name="Codex Session", account_name="acct1",
                engine="codex", engine_session_id="th-1", status=status,
                cwd="/repo/s1", name_is_auto=False)
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def _codex_driver(orch, sid, tasks, *, engine="codex"):
    """A stand-in for CodexAgentDriver's one relevant surface."""
    async def open_background_work():
        return list(tasks)

    drv = types.SimpleNamespace(engine=engine, open_background_work=open_background_work,
                                last_pr_resolution=None, _turn_buffer=[])
    orch._drivers[sid] = drv
    return drv


def test_a_codex_session_with_a_live_terminal_parks(tmp_path):
    """The gap this closes: a Codex turn that ends with background work running used
    to read "Your turn", because the only parked check scanned a Claude transcript."""
    async def run():
        orch = await _codex_orch(tmp_path)
        _codex_driver(orch, "s1", [_codex_task("tail -f build.log")])
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "waiting_external"


def test_a_codex_session_with_no_terminals_is_not_parked(tmp_path):
    async def run():
        orch = await _codex_orch(tmp_path)
        _codex_driver(orch, "s1", [])
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "done"


def test_a_failed_read_does_not_park_the_session(tmp_path):
    """A stuck "Waiting…" needs a restart to clear; a missed one clears itself on the
    next turn. So an app-server that cannot answer must read as "not parked"."""
    async def run():
        orch = await _codex_orch(tmp_path)
        drv = _codex_driver(orch, "s1", [])

        async def boom():
            raise RuntimeError("app-server is down")

        drv.open_background_work = boom
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "done"


def test_a_slow_app_server_cannot_hold_the_turn_boundary_open(tmp_path):
    """The read sits on the turn-end status write. A paginated read inherits the
    client's per-request timeout once per page, so it needs its own ceiling — a
    "Waiting…" overlay is not worth stalling a turn boundary for."""
    async def run():
        import server.sessions.orchestrator as orch_mod
        orch = await _codex_orch(tmp_path)
        drv = _codex_driver(orch, "s1", [])
        started = asyncio.Event()

        async def never_answers():
            started.set()
            await asyncio.sleep(3600)

        drv.open_background_work = never_answers
        orch_mod._BG_WORK_READ_TIMEOUT_S = 0.05
        try:
            await asyncio.wait_for(orch._persist_status("s1", "done"), timeout=5)
        finally:
            orch_mod._BG_WORK_READ_TIMEOUT_S = 15.0
        return started.is_set(), orch.record("s1").status
    asked, status = asyncio.run(run())
    assert asked is True     # it really did try
    assert status == "done"  # ...and gave up rather than parking or hanging


def test_a_parked_codex_turn_that_asks_the_user_says_answer_needed(tmp_path):
    """The prose safeguard is engine-neutral now: a Codex turn holding a terminal open
    AND handing a decision back must not be masked as "Waiting…". Its prose comes from
    the driver's turn buffer, since Codex has no transcript to read."""
    async def run():
        orch = await _codex_orch(tmp_path)
        drv = _codex_driver(orch, "s1", [_codex_task()])
        drv._turn_buffer = [
            ev.UserMessage(turn_id="t1", text="ship it"),
            ev.TextDelta(turn_id="t1",
                         text="Build is running. Two options: A or B. "
                              "Which would you prefer?"),
        ]
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "waiting_question"


def test_a_codex_row_unparks_across_a_daemon_restart(tmp_path):
    """The restore half, answered honestly. A `waiting_external` Codex row rehydrates
    to `done` because the terminals it was parked on were grandchildren of an
    app-server this daemon has not started — the boot sweep reaps any that outlived
    the last one. No driver, no live work, no lingering "Waiting…" card."""
    async def run():
        orch = await _codex_orch(tmp_path, status="waiting_external")
        return orch.record("s1").status
    assert asyncio.run(run()) == "done"


def test_a_drain_snapshots_codex_terminals_as_non_replayable(tmp_path):
    """What the restart turn is built from. The snapshot must survive to the DB with
    ``replayable`` intact — that flag is the whole difference between reporting a lost
    process and re-running its command line."""
    async def run():
        from sqlalchemy import select
        orch = await _codex_orch(tmp_path)
        _codex_driver(orch, "s1", [_codex_task("npm run migrate")])
        await orch._snapshot_open_tasks()
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            return row.pending_rearm
    snap = asyncio.run(run())
    assert len(snap) == 1
    assert snap[0]["replayable"] is False
    assert snap[0]["input"]["command"] == "npm run migrate"
    # And the turn built from it reports rather than instructs.
    prompt = build_rearm_prompt([OpenBgTask.from_dict(d) for d in snap])
    assert "were NOT restarted" in prompt
    assert "Relaunch each one" not in prompt


def test_rearm_delivers_the_notice_once_and_clears_the_snapshot(tmp_path):
    """"…without duplicate side effects": the boot pass and a later attach share one
    idempotent path, so a session is told about its lost terminals exactly once."""
    async def run():
        from sqlalchemy import select
        orch = await _codex_orch(tmp_path)
        sent: list[str] = []
        drv = _codex_driver(orch, "s1", [])
        drv.send_prompt = lambda text: sent.append(text) or asyncio.sleep(0)

        async def ensure_driver(sid):
            return drv

        orch.ensure_driver = ensure_driver
        snap = [_codex_task("npm run dev").to_dict()]
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(sa_update(DbSession).where(DbSession.id == "s1")
                             .values(pending_rearm=snap))
        first = await orch.rearm_background_tasks()
        second = await orch.rearm_background_tasks()   # idempotent re-call
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == "s1"))).scalar_one()
        return first, second, sent, row.pending_rearm
    first, second, sent, remaining = asyncio.run(run())
    assert (first, second) == (1, 0)
    assert len(sent) == 1
    assert "npm run dev" in sent[0] and "were NOT restarted" in sent[0]
    assert remaining is None
