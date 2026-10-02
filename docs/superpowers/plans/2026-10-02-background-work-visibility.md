# Background Work Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a session's open background shells, Monitors, Codex terminals and armed wakeup — the things that make it read "Waiting…" — as a separate "Background" chip/list in the session view and on the session card, with Stop for live shells.

**Architecture:** The server persists a `background_work` snapshot (rendered from the existing `OpenBgTask` scan + wakeup scan) on the session record/row and serializes it as `Session.backgroundWork`; the translator additionally emits `backgroundTaskStarted/Ended` wire events for non-agent tasks. The web reducer tracks the live events, `selectBackgroundWork` merges them with the snapshot, and `SessionMeta`/`SessionCard` render the result beside the existing sub-agent surface.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (nullable JSON column, auto-reconciled) · claude-agent-sdk 0.2.105 task messages · React + TS + vitest + testing-library.

**Spec:** `docs/superpowers/specs/2026-10-02-background-work-visibility-design.md`

## Global Constraints

- Run every Python command with the main checkout's env: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`, and `env -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_TLS_CA -u VIBERPIT_SESSIOND_TLS_CERT -u VIBERPIT_SESSIOND_TLS_KEY -u VIBERPIT_SESSIOND_CLIENT_ALLOWLIST -u VIBERPIT_TAILNET_ALLOWLIST VIBERPIT_DEV=1` in front of any `server/tests/` run.
- Web commands run as `(cd web && npm run <script>)` subshells — never a persisted `cd`. `web/node_modules` may need to be symlinked from `/home/tony/GitHub/viberpit/web/node_modules`.
- Never `func.now()`; no new `ccswitch` spellings; new columns nullable (JSON, `none_as_null=True`).
- Commit messages via `git commit -F /tmp/<file>`; end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Wire names are camelCase; event `type` strings: `backgroundTaskStarted`, `backgroundTaskEnded`; record/HTTP key: `backgroundWork`.

## Review Focus

1. A **foreground** Bash (`task_started` with `is_backgrounded: false`, no `run_in_background`) must never appear as a background row — Task 5's selector test `hides a live task that was not launched in the background`.
2. The kill sequence delivers **two** terminal frames (`task_updated(killed)` then `task_notification(stopped)`) — Task 4's reducer test `ended is idempotent` and Task 3's translator test `kill sequence emits one ended per frame but the map is evicted once`.
3. A snapshot item the live stream **already saw end** must be hidden, not shown running — Task 5's selector test `drops a snapshot item the live stream saw end`.
4. An armed wakeup whose arming entry has **no parseable timestamp** must still read as armed (not silently dropped) — Task 1's `pending_wakeup` test and Task 6's list test `shows an armed wakeup with no fire time`.
5. A record from an **older daemon** over RPC has no `backgroundWork`/`subAgents` key — Task 2's `from_dict` test.

---

### Task 1: Snapshot primitives in `background_tasks.py`

**Files:**
- Modify: `server/sessions/background_tasks.py` (`OpenBgTask` ~L66-101, `_launch` ~L151-161, scan loop ~L195-282, `scan_pending_wakeup` ~L337-380)
- Test: `server/tests/test_background_tasks_scan.py` (append)

**Interfaces:**
- Produces: `OpenBgTask.started_at: str | None`; `PendingWakeup(fire_at: datetime | None)`; `pending_wakeup(jsonl_path, *, now=None) -> PendingWakeup | None`; `background_work_snapshot(tasks: list[OpenBgTask], wakeup: PendingWakeup | None) -> dict`; `EMPTY_BACKGROUND_WORK: dict`.
- `scan_pending_wakeup` keeps its signature and becomes `pending_wakeup(...) is not None`.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_background_tasks_scan.py`; the file already has `_assistant_tool`, `_tool_result`, `_ack`, `_user`, `_notif` helpers and a `_write(tmp_path, entries)`-style pattern — reuse whatever helper the file uses to write a jsonl; if none exists, add this one:)

```python
def _jsonl(tmp_path, entries):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(p)


def test_open_task_carries_the_launch_timestamp(tmp_path):
    launch = _assistant_tool("tu1", "Bash", {"command": "sleep 99", "run_in_background": True,
                                             "description": "long sleep"})
    launch["timestamp"] = "2026-10-02T11:00:00.000Z"
    path = _jsonl(tmp_path, [launch, _tool_result("tu1")])
    [t] = scan_open_background_tasks(path)
    assert t.started_at == "2026-10-02T11:00:00.000Z"
    assert OpenBgTask.from_dict(t.to_dict()).started_at == t.started_at
    # a pre-#2635 snapshot has no key → None, not a crash
    assert OpenBgTask.from_dict({"tool_use_id": "x", "kind": "task"}).started_at is None


def test_pending_wakeup_reports_the_fire_time(tmp_path):
    from server.sessions.background_tasks import pending_wakeup
    arm = _assistant_tool("tu1", "ScheduleWakeup", {"delaySeconds": 600, "prompt": "p", "reason": "r"})
    arm["timestamp"] = "2026-10-02T11:00:00.000Z"
    path = _jsonl(tmp_path, [_user("go"), arm, _tool_result("tu1")])
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
    path = _jsonl(tmp_path, [_user("go"), arm, _tool_result("tu1")])
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `<venv>/bin/python -m pytest server/tests/test_background_tasks_scan.py -q -k "launch_timestamp or pending_wakeup or snapshot_shape"`
Expected: FAIL — `ImportError: cannot import name 'pending_wakeup'` / `TypeError: unexpected keyword 'started_at'`.

- [ ] **Step 3: Implement**

In `OpenBgTask` add after `replayable`:
```python
    #: ISO-8601 ``timestamp`` of the transcript entry that launched it (the CLI stamps
    #: every entry); None for a snapshot written before #2635 or an entry without one.
    started_at: str | None = None
```
Add `"started_at": self.started_at` to `to_dict()` and `started_at=d.get("started_at")` to `from_dict()`.

Change `_launch`:
```python
def _launch(tid: str, name: str, inp: dict, started_at: str | None = None) -> OpenBgTask:
    ...
    return OpenBgTask(tool_use_id=tid, kind=kind, name=name, input=inp,
                      description=str(inp.get("description") or inp.get("summary") or ""),
                      replayable=name not in _AGENT_TOOLS, started_at=started_at)
```
In the scan loop: `pending: dict[str, tuple[str, dict, str | None]] = {}`; at the assistant site `ts = o.get("timestamp") if isinstance(o.get("timestamp"), str) else None`, then `launches[tid] = _launch(tid, name, inp, ts)` and `pending[tid] = (name, inp, ts)`; at the ack site `launches[tid] = _launch(tid, call[0], call[1], call[2])` (the `TaskStop` branch keeps reading `call[0]`/`call[1]`).

Replace `scan_pending_wakeup` body with:
```python
@dataclass
class PendingWakeup:
    """An armed ``ScheduleWakeup``. ``fire_at`` is None when the arming entry carried no
    parseable timestamp — still armed, just untimed."""
    fire_at: datetime | None


def pending_wakeup(jsonl_path: str, *, now: datetime | None = None) -> PendingWakeup | None:
    """<move scan_pending_wakeup's docstring here verbatim>"""
    armed_at: datetime | None = None
    delay: float | None = None
    for o in _iter_jsonl(jsonl_path):
        if o.get("type") == "user" and not is_tool_result_entry(o):
            armed_at, delay = None, None
        else:
            d = _wakeup_delay_seconds(o)
            if d is not None:
                armed_at, delay = _parse_ts(o.get("timestamp")), d
    if delay is None:
        return None
    if armed_at is None:
        return PendingWakeup(fire_at=None)
    fire_at = armed_at + timedelta(seconds=delay)
    now = now or datetime.now(timezone.utc)
    if now > fire_at + timedelta(seconds=_WAKEUP_GRACE_SEC):
        return None
    return PendingWakeup(fire_at=fire_at)


def scan_pending_wakeup(jsonl_path: str, *, now: datetime | None = None) -> bool:
    """True iff the agent is parked on a self-scheduled wakeup — see ``pending_wakeup``."""
    return pending_wakeup(jsonl_path, now=now) is not None


EMPTY_BACKGROUND_WORK: dict = {"items": [], "wakeup": False, "wakeupAt": None}


def background_work_snapshot(tasks: list[OpenBgTask], wakeup: PendingWakeup | None) -> dict:
    """The browser's view of a session's open background work (#2635): the shells /
    Monitors / Codex terminals behind "Waiting…", plus the armed wakeup. Background
    AGENTS are excluded — the sub-agent surface already shows them. Mirrors
    ``BackgroundWork`` in web/src/api/types.ts."""
    items = []
    for t in tasks:
        if t.kind == "agent":
            continue
        cmd = t.input.get("command") if isinstance(t.input, dict) else None
        items.append({"toolUseId": t.tool_use_id, "kind": t.kind, "name": t.name,
                      "description": t.description or (cmd if isinstance(cmd, str) else ""),
                      "startedAt": t.started_at})
    return {"items": items, "wakeup": wakeup is not None,
            "wakeupAt": wakeup.fire_at.isoformat() if wakeup and wakeup.fire_at else None}
```
(`dataclass`, `datetime`, `timedelta`, `timezone` are already imported in this module — check the header.)

- [ ] **Step 4: Run the whole file + mypy**

Run: `<venv>/bin/python -m pytest server/tests/test_background_tasks_scan.py -q && <venv>/bin/python -m mypy server/sessions/background_tasks.py --ignore-missing-imports`
Expected: all pass (38 existing + 4 new), mypy clean.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/background_tasks.py server/tests/test_background_tasks_scan.py
git commit -F /tmp/c1.txt   # "feat(sessions): background_work_snapshot + pending_wakeup fire time (#2635)"
```

---

### Task 2: Persist and serialize `backgroundWork` (record, row, RPC, HTTP, orchestrator)

**Files:**
- Modify: `server/sessions/types.py` (SessionRecord field ~L85, `to_dict` ~L197, `from_dict` ~L266)
- Modify: `server/db/models.py` (after `sub_agents`, ~L127)
- Create: `server/db/migrations/versions/0076_sessions_background_work.py`
- Modify: `server/api/sessions.py` (~L267)
- Modify: `server/sessions/engine.py` (`AgentDriverConfig` ~L257)
- Modify: `server/sessions/orchestrator.py` (`_persist_status` ~L5485-5600, `_session_is_parked` ~L6287, driver config ~L2315, `_persist_sub_agents` ~L14384, rehydrate heal ~L15050)
- Test: `server/tests/test_background_work_record.py` (new), `server/tests/test_queue_stall_reason.py` (~L206 add key)

**Interfaces:**
- Consumes: `background_work_snapshot`, `pending_wakeup`, `EMPTY_BACKGROUND_WORK`, `scan_open_background_tasks(path, treat_killed_as_open=False)` from Task 1.
- Produces: `SessionRecord.background_work: dict | None`; wire key `backgroundWork` (record RPC and HTTP, HTTP always a dict, default `EMPTY_BACKGROUND_WORK`); `SessionOrchestrator._background_work_snapshot(session_id) -> dict`; `_refresh_background_work(session_id)` (sync, spawns `_persist_background_work`); `AgentDriverConfig.on_background_work_changed: Callable[[str], None] | None`.

- [ ] **Step 1: Failing tests** — create `server/tests/test_background_work_record.py`:

```python
"""SessionRecord carries background_work (and sub_agents) across the daemon→edge RPC
(#2635). sub_agents was never in to_dict/from_dict, so fleet-mode cards showed no
sub-agent rows — fixed in the same hunk."""
from server.sessions.types import SessionRecord, AgentEngine


def _rec(**over):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                cwd="/tmp", status="done")
    base.update(over)
    return SessionRecord(**base)


def test_background_work_and_sub_agents_round_trip():
    snap = {"items": [{"toolUseId": "tu1", "kind": "task", "name": "Bash",
                       "description": "pytest", "startedAt": None}],
            "wakeup": False, "wakeupAt": None}
    agents = [{"agentId": "tu9", "parentAgentId": None, "agentType": "Explore",
               "description": "d", "status": "running"}]
    d = _rec(background_work=snap, sub_agents=agents).to_dict()
    assert d["backgroundWork"] == snap and d["subAgents"] == agents
    back = SessionRecord.from_dict(d)
    assert back.background_work == snap and back.sub_agents == agents


def test_from_dict_tolerates_an_older_daemon_without_the_keys():
    d = _rec().to_dict()
    d.pop("backgroundWork"); d.pop("subAgents")
    back = SessionRecord.from_dict(d)
    assert back.background_work is None and back.sub_agents is None
```
(Adjust the required `SessionRecord` ctor fields to whatever `_rec` needs — copy from `server/tests/test_pending_model_reason_column.py`'s record builder.)

In `server/tests/test_queue_stall_reason.py` `expected_types` add `"backgroundWork": dict,`.

- [ ] **Step 2: Run to verify failure**

Run: `<venv>/bin/python -m pytest server/tests/test_background_work_record.py server/tests/test_queue_stall_reason.py -q`
Expected: FAIL — `TypeError: unexpected keyword 'background_work'`, `missing pre-existing field 'backgroundWork'`.

- [ ] **Step 3: Record + row + migration + serializer**

`server/sessions/types.py`, after `sub_agents`:
```python
    # Open background work snapshot ({items, wakeup, wakeupAt} — background_tasks.
    # background_work_snapshot), refreshed at turn end and on task start/end. None =
    # nothing open. Drives the "Background" chip (#2635).
    background_work: dict | None = None
```
`to_dict`: add `"subAgents": self.sub_agents, "backgroundWork": self.background_work,`. `from_dict`: add `sub_agents=d.get("subAgents"), background_work=d.get("backgroundWork"),`.

`server/db/models.py`, after `sub_agents`:
```python
    # Open background work snapshot ({items, wakeup, wakeupAt}) behind a "Waiting…"
    # session — background shells / Monitors / Codex terminals and the armed wakeup —
    # refreshed at turn end (#2635). Nullable so init_db backfills it; none_as_null so
    # clearing persists SQL NULL.
    background_work: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None)
```
Migration `0076_sessions_background_work.py` (copy `0075_quiet_hours_timezone.py`'s shape): `revision = "0076_sessions_background_work"`, `down_revision = "0075_quiet_hours_timezone"`, `op.add_column("sessions", sa.Column("background_work", sa.JSON(), nullable=True))` / drop.

`server/api/sessions.py` next to `"subAgents"`:
```python
        "backgroundWork": getattr(record, "background_work", None) or EMPTY_BACKGROUND_WORK,
```
(import `EMPTY_BACKGROUND_WORK` from `server.sessions.background_tasks` at the top.)

- [ ] **Step 4: Orchestrator — snapshot, persist, refresh, heal**

Add near `_open_background_work`:
```python
    async def _background_work_snapshot(self, session_id: str) -> dict:
        """What a parked session is waiting on, for the browser (#2635). Claude: the
        transcript scan with killed tasks EXCLUDED (dead, not waiting — the same read
        ``is_parked`` makes) plus the armed wakeup. Other engines: the driver's live
        list, no wakeup half. Never raises; a failed read is "nothing open"."""
        try:
            if self._engine_of(session_id) is AgentEngine.CLAUDE_CODE:
                from .background_tasks import pending_wakeup
                path = await asyncio.to_thread(self.transcript_path, session_id)
                if not path:
                    return EMPTY_BACKGROUND_WORK
                tasks = await asyncio.to_thread(
                    scan_open_background_tasks, path, treat_killed_as_open=False)
                wakeup = await asyncio.to_thread(pending_wakeup, path)
                return background_work_snapshot(tasks, wakeup)
            return background_work_snapshot(await self._open_background_work(session_id), None)
        except Exception:
            log.debug("bg_work.snapshot_failed sid=%s", session_id, exc_info=True)
            return EMPTY_BACKGROUND_WORK

    def _refresh_background_work(self, session_id: str) -> None:
        """Driver ``on_background_work_changed`` callback: a background shell/Monitor
        started or ended. Scheduled, never awaited — fired from inside the pump."""
        self._spawn(self._persist_background_work(session_id), name="background-work-refresh")

    async def _persist_background_work(self, session_id: str) -> None:
        try:
            snapshot = await self._background_work_snapshot(session_id)
            rec = self._records.get(session_id)
            if rec is not None:
                rec.background_work = snapshot
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                                 .values(background_work=snapshot))
        except Exception:
            log.debug("persist_background_work failed (non-fatal) session=%s", session_id, exc_info=True)
```
Import `background_work_snapshot, EMPTY_BACKGROUND_WORK` alongside the existing `scan_open_background_tasks` import (~L134).

In `_persist_status`: inside `if completed_turn:` keep `parked = await self._session_is_parked(session_id)` as the predicate (do not change `_session_is_parked`; tests patch it), and set
`background_work = await self._background_work_snapshot(session_id) if parked else None`; initialise `background_work: dict | None = None` and `completed_turn` handling so that **only when `completed_turn`** you add `rec.background_work = background_work` (next to `rec.hand_back_reason = …`) and `values["background_work"] = background_work` (next to `values["hand_back_reason"]`). A non-finished status leaves the stored snapshot alone.

Rehydrate heal (~L15050): where `healed[row.id] = "done"` is set, also `self._records[row.id].background_work = None`; in the persist block below, the per-row update for healed rows adds `background_work=None` (read the loop: it builds `.values(status=…)` — add the key there for rows in `healed`).

Driver config: `server/sessions/engine.py` after `on_sub_agents_changed`:
```python
    #: Fired (synchronously, with the session id) when a background shell/Monitor task
    #: starts or ends, so the orchestrator can refresh the persisted background-work
    #: snapshot (#2635). Only Claude calls it.
    on_background_work_changed: Callable[[str], None] | None = None
```
Orchestrator driver config (~L2315): add `on_background_work_changed=self._refresh_background_work,`.

- [ ] **Step 5: Run**

Run: `<venv>/bin/python -m pytest server/tests/test_background_work_record.py server/tests/test_queue_stall_reason.py server/tests/test_background_tasks_scan.py server/tests/test_schema_reconcile_coverage.py server/tests/test_sessions_api*.py -q -x && <venv>/bin/python -m mypy server/ --ignore-missing-imports && <venv>/bin/python -m ruff check src/ server/`
Expected: pass, clean.

- [ ] **Step 6: Commit** — "feat(sessions): persist a backgroundWork snapshot on the record and serialize it; subAgents rides the RPC too (#2635)"

---

### Task 3: Translator + driver — live `backgroundTaskStarted/Ended`

**Files:**
- Modify: `server/sessions/events.py` (after `SubAgentEnded`, ~L188)
- Modify: `server/sessions/translator.py` (`__init__` ~L53, `_sub_agent_notice` L77-131)
- Modify: `server/sessions/claude_driver.py` (ctor kwarg ~L286/337, factory ~L2268, `_handle_msg` ~L1468-1480, `stop_subagent` docstring ~L1965)
- Modify: `server/sessions/session_actions.py` (~L60 message)
- Test: `server/tests/test_translator_subagent_notices.py` (edit 1, add 4), `server/tests/test_driver_subagent_stop.py` (add 1)

**Interfaces:**
- Produces: `ev.BackgroundTaskStarted(tool_use_id, task_id, description, task_type, backgrounded=True)` → wire `{"type":"backgroundTaskStarted","toolUseId","taskId","description","taskType","backgrounded","ts"}`; `ev.BackgroundTaskEnded(tool_use_id, task_id, status)` → `{"type":"backgroundTaskEnded","toolUseId","taskId","status","ts"}` with status in `completed|failed|stopped`.
- Driver: `_subagent_tasks[tool_use_id] = task_id` for background tasks too, so `stop_subagent(tool_use_id)` works for shells.

- [ ] **Step 1: Failing tests.** In `test_translator_subagent_notices.py` replace `test_task_started_non_agent_type_is_ignored_even_with_tool_use_id` with:

```python
def test_local_bash_task_started_becomes_background_task_started():
    """A backgrounded Bash / Monitor is a `local_bash` task whose task_started carries the
    Bash call's tool_use_id (claude 2.1.287, read from the binary 2026-10-02). It is not a
    sub-agent — it gets its own event (#2635)."""
    _, out = _run([f.task_started(TASK, TID, description="pytest -q", task_type="local_bash",
                                  backgrounded=True)])
    assert len(out) == 1 and isinstance(out[0], ev.BackgroundTaskStarted)
    e = out[0]
    assert (e.tool_use_id, e.task_id, e.description, e.task_type, e.backgrounded) == (
        TID, TASK, "pytest -q", "local_bash", True)
    w = e.to_wire()
    assert w["type"] == "backgroundTaskStarted" and w["toolUseId"] == TID
    assert w["taskType"] == "local_bash" and w["backgrounded"] is True and isinstance(w["ts"], float)


def test_background_task_notification_resolves_tool_use_id_from_the_map():
    """The CLI's task_notification carries no task_type; the map started by task_started
    is what says this is a shell, even when the notice itself names the tool_use_id."""
    _, out = _run([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True),
                   f.task_notification(TASK, None, status="completed", summary="ok")])
    e = out[1]
    assert isinstance(e, ev.BackgroundTaskEnded)
    assert (e.tool_use_id, e.task_id, e.status) == (TID, TASK, "completed")
    assert e.to_wire() == {"type": "backgroundTaskEnded", "toolUseId": TID, "taskId": TASK,
                           "status": "completed", "ts": e.ts}


def test_background_kill_sequence_emits_one_ended_per_frame_but_evicts_once():
    tr, out = _run([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True),
                    f.task_updated(TASK, "killed"),
                    f.task_notification(TASK, TID, status="stopped", summary="")])
    ended = [e for e in out if isinstance(e, ev.BackgroundTaskEnded)]
    assert [e.status for e in ended] == ["stopped", "stopped"]
    assert TASK not in tr._bg_task_tool


def test_background_progress_emits_nothing():
    _, out = _run([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True),
                   f.task_progress(TASK, TID)])
    assert len(out) == 1


def test_agent_task_path_is_unchanged_by_the_background_map():
    """A local_agent still becomes SubAgentStarted and never touches _bg_task_tool."""
    tr, out = _run([f.task_started(TASK, TID)])
    assert isinstance(out[0], ev.SubAgentStarted) and tr._bg_task_tool == {}
```
(`fake_sdk.task_notification`'s `tool_use_id=None` case with the map, and `TID` case: both covered above.)

In `test_driver_subagent_stop.py` add, mirroring `test_started_registers_and_ended_unregisters_the_task` (read it first for how frames are injected and the client faked):
```python
def test_background_shell_registers_in_the_task_map_and_stop_kills_it():
    """A backgrounded Bash shares the Task map so POST /agents/{toolUseId}/stop stops it."""
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    seen: list[str] = []
    d._on_background_work_changed = seen.append

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True)])
        await _settle(d, lambda: d.subagent_task_id(TID) == TASK)
        assert seen == ["s1"]
        assert await d.stop_subagent(TID) == TASK
        assert clients[0].stopped_tasks == [TASK]
        clients[0].inject([f.task_notification(TASK, TID, status="stopped", summary="")])
        await _settle(d, lambda: d.subagent_task_id(TID) is None)
        assert seen == ["s1", "s1"]
        await d.aclose()
    _run(run())
```
(`FakeSDKClient.inject(list)` and `.stopped_tasks` are the names the file's existing tests use; `_driver` builds the driver without the callback, hence the direct attribute assignment.)

- [ ] **Step 2: Run to verify failure**

Run: `<venv>/bin/python -m pytest server/tests/test_translator_subagent_notices.py server/tests/test_driver_subagent_stop.py -q`
Expected: FAIL — `AttributeError: module 'server.sessions.events' has no attribute 'BackgroundTaskStarted'`.

- [ ] **Step 3: Events**

`server/sessions/events.py` after `SubAgentEnded`:
```python
# ── Background task lifecycle (#2635) ──
# A backgrounded Bash shell / Monitor (`local_bash`) or Workflow (`local_workflow`) task,
# keyed by the launching call's tool-use id — the key the web reducer's
# `state.backgroundTasks` and the snapshot's `items[].toolUseId` share. Sourced from the
# CLI's system/task_* notices (translator.py), never from the transcript.

@dataclass
class BackgroundTaskStarted:
    tool_use_id: str; task_id: str; description: str; task_type: str
    backgrounded: bool = True
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "backgroundTaskStarted", "toolUseId": self.tool_use_id,
                               "taskId": self.task_id, "description": self.description,
                               "taskType": self.task_type, "backgrounded": self.backgrounded,
                               "ts": self.ts}

@dataclass
class BackgroundTaskEnded:
    tool_use_id: str; task_id: str; status: str   # "completed" | "failed" | "stopped"
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "backgroundTaskEnded", "toolUseId": self.tool_use_id,
                               "taskId": self.task_id, "status": self.status, "ts": self.ts}
```

- [ ] **Step 4: Translator**

`__init__`: after `self._task_tool`:
```python
        # Background (non-agent) tasks: CLI task id -> the launching tool-use id. Same
        # lifetime as _task_tool, same reason (#2635).
        self._bg_task_tool: dict[str, str] = {}
```
Rewrite `_sub_agent_notice` (keep the name and call site) so each branch first handles the background map:
```python
    def _sub_agent_notice(self, msg) -> list:
        """system/task_* notices → SubAgent* events for a Task sub-agent (spec §4.2), or
        BackgroundTask* events for a backgrounded shell / Monitor / Workflow (#2635). A
        notice with no tool-use id to attach to (and no map entry) is dropped."""
        if isinstance(msg, TaskStartedMessage):
            tuid = msg.tool_use_id
            if not tuid:
                return []
            d = msg.data or {}
            if msg.task_type not in (None, "local_agent"):
                self._bg_task_tool[msg.task_id] = tuid
                return [ev.BackgroundTaskStarted(
                    tuid, msg.task_id, msg.description or "", str(msg.task_type),
                    backgrounded=bool(d.get("is_backgrounded", True)))]
            self._task_tool[msg.task_id] = tuid
            ... (existing SubAgentStarted construction unchanged)
        if isinstance(msg, TaskProgressMessage):
            if msg.task_id in self._bg_task_tool:
                return []          # shells report no progress worth a frame
            ... (existing)
        if isinstance(msg, TaskUpdatedMessage):
            if msg.status not in TERMINAL_TASK_STATUSES:
                return []
            btuid = self._bg_task_tool.pop(msg.task_id, None)
            if btuid:
                return [ev.BackgroundTaskEnded(btuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]
            ... (existing)
        if isinstance(msg, TaskNotificationMessage):
            btuid = self._bg_task_tool.pop(msg.task_id, None)
            if btuid:
                return [ev.BackgroundTaskEnded(btuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]
            ... (existing)
        return []
```
Kill sequence: `task_updated(killed)` pops the map and emits `stopped`; the following `task_notification(stopped)` carries `tool_use_id=TID` with no map entry left, and it must **still** emit `BackgroundTaskEnded` rather than fall through to the sub-agent path. Keep a second, append-only set `self._bg_tool_ids: set[str]` (add the tool-use id in the `task_started` branch; one id per background launch, never cleared) and in the notification branch, after the `btuid` pop, add: `if not btuid and msg.tool_use_id and msg.tool_use_id in self._bg_tool_ids: return [ev.BackgroundTaskEnded(msg.tool_use_id, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]`. Confirm the existing sub-agent kill-sequence tests in the file still pass.

- [ ] **Step 5: Driver**

Ctor: add `on_background_work_changed=None` kwarg (~L286) and `self._on_background_work_changed = on_background_work_changed` (~L337); factory (~L2268) passes `on_background_work_changed=config.on_background_work_changed`. In `_handle_msg`, after the `SubAgentStarted/Ended` block:
```python
            if isinstance(e, (ev.BackgroundTaskStarted, ev.BackgroundTaskEnded)):
                if isinstance(e, ev.BackgroundTaskStarted):
                    self._subagent_tasks[e.tool_use_id] = e.task_id
                else:
                    self._subagent_tasks.pop(e.tool_use_id, None)
                if self._on_background_work_changed is not None:
                    try:
                        self._on_background_work_changed(self.session_id)
                    except Exception:  # noqa: BLE001 - a snapshot refresh must never break the pump
                        log.debug("driver.on_background_work_changed failed session=%s",
                                  self.session_id, exc_info=True)
```
Update `stop_subagent`'s docstring: "Stop ONE running task — a Task sub-agent or a backgrounded shell/Monitor (#2635) — via stop_task". In `session_actions.stop_subagent` change the 404 text to `"That task is not running — it may have already finished."` (grep `server/tests` for the old string and update).

- [ ] **Step 6: Check the WS bridge forwards unknown event types.** `grep -n "to_wire\|type\b" server/bridge/ws.py | head` — confirm events are forwarded generically (no allowlist). If there is one, add both new types.

- [ ] **Step 7: Run**

Run: `<venv>/bin/python -m pytest server/tests/test_translator_subagent_notices.py server/tests/test_driver_subagent_stop.py server/tests/test_driver_subagent_attribution.py server/tests/test_session_actions.py server/tests/test_sessions_api_subagent_stop.py -q && <venv>/bin/python -m mypy server/ --ignore-missing-imports && <venv>/bin/python -m ruff check src/ server/`
Expected: pass, clean.

- [ ] **Step 8: Commit** — "feat(sessions): backgroundTaskStarted/Ended wire events for shells and Monitors; shells share the stop map (#2635)"

---

### Task 4: Web — event types + reducer state

**Files:**
- Modify: `web/src/session/events.ts` (~L77 add two interfaces, union ~L84, flags ~L116)
- Modify: `web/src/session/reducer.ts` (types ~L86, state ~L141, initial ~L170, cases after `subAgentEnded` ~L1422)
- Test: `web/src/session/events.test.ts`, `web/src/session/reducer.backgroundTask.test.ts` (new)

**Interfaces:**
- Produces: `EvBackgroundTaskStarted`, `EvBackgroundTaskEnded`; `BackgroundTaskStatus = 'running'|'completed'|'failed'|'stopped'`; `BackgroundTaskState { taskId; description; taskType; backgrounded; status; startedAt?; endedAt? }`; `TranscriptState.backgroundTasks: Record<string, BackgroundTaskState>`.

- [ ] **Step 1: Failing tests.** In `events.test.ts` extend the `it.each` list with `'backgroundTaskStarted', 'backgroundTaskEnded'`. Create `reducer.backgroundTask.test.ts`:

```ts
// @vitest-environment node
/** Background shell/Monitor task lifecycle (#2635): live-only state keyed by the
 *  launching call's tool-use id, preserved across replayReset. */
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

type Action = Parameters<typeof transcriptReducer>[1]
const reduce = (events: Action[], initial: TranscriptState = initialTranscript) =>
  events.reduce((s, ev) => transcriptReducer(s, ev), initial)

const TID = 'toolu_B'
const started: SessionEvent = { type: 'backgroundTaskStarted', toolUseId: TID, taskId: 'bg1', description: 'pytest -q', taskType: 'local_bash', backgrounded: true, ts: 1000 }
const killed: SessionEvent = { type: 'backgroundTaskEnded', toolUseId: TID, taskId: 'bg1', status: 'stopped', ts: 3000 }
const stoppedAgain: SessionEvent = { type: 'backgroundTaskEnded', toolUseId: TID, taskId: 'bg1', status: 'completed', ts: 3001 }

describe('background task lifecycle', () => {
  it('started records a running task', () => {
    expect(reduce([started]).backgroundTasks[TID]).toEqual({
      taskId: 'bg1', description: 'pytest -q', taskType: 'local_bash', backgrounded: true,
      status: 'running', startedAt: 1000,
    })
  })
  it('ended is idempotent and never regresses the first terminal status', () => {
    const s = reduce([started, killed, stoppedAgain])
    expect(s.backgroundTasks[TID]).toMatchObject({ status: 'stopped', endedAt: 3000 })
  })
  it('ended before started still records the task', () => {
    expect(reduce([killed]).backgroundTasks[TID]).toMatchObject({ taskId: 'bg1', status: 'stopped', endedAt: 3000 })
  })
  it('replayReset preserves live task state (the replay never re-emits task notices)', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, { type: 'replayComplete' }, started, { type: 'replayReset' }])
    expect(s.backgroundTasks[TID]?.status).toBe('running')
  })
})
```

- [ ] **Step 2: Run** — `(cd web && npx vitest run src/session/events.test.ts src/session/reducer.backgroundTask.test.ts)` → FAIL (type errors / missing case).

- [ ] **Step 3: Implement.** `events.ts` after `EvSubAgentEnded`:
```ts
// Background shell / Monitor / Workflow task lifecycle (#2635), sourced from the CLI's
// system/task_* notices for non-agent tasks. `toolUseId` is the launching Bash/Monitor
// call — the key shared with `Session.backgroundWork.items[].toolUseId`. Mirrors
// BackgroundTaskStarted / BackgroundTaskEnded (server/sessions/events.py).
export interface EvBackgroundTaskStarted { type: 'backgroundTaskStarted'; toolUseId: string; taskId: string; description: string; taskType: string; backgrounded: boolean; ts?: number }
export interface EvBackgroundTaskEnded   { type: 'backgroundTaskEnded';   toolUseId: string; taskId: string; status: 'completed' | 'failed' | 'stopped'; ts?: number }
```
Add both to the `SessionEvent` union and `backgroundTaskStarted: true, backgroundTaskEnded: true,` to `SESSION_EVENT_TYPE_FLAGS`.

`reducer.ts` after `SubThread`:
```ts
export type BackgroundTaskStatus = 'running' | 'completed' | 'failed' | 'stopped'
/** Live state of a backgrounded shell / Monitor / Workflow task (backgroundTaskStarted/
 *  Ended). Live-only: the replay journals no task notices, so the resting view comes
 *  from `Session.backgroundWork` — see selectBackgroundWork. */
export interface BackgroundTaskState {
  taskId: string; description: string; taskType: string; backgrounded: boolean
  status: BackgroundTaskStatus
  startedAt?: number; endedAt?: number
}
```
State: `backgroundTasks: Record<string, BackgroundTaskState>   // keyed by the launching tool-use id (#2635)`; initial `backgroundTasks: {}`. Cases after `subAgentEnded`:
```ts
    // ── Background shell / Monitor lifecycle (#2635) ─────────────────────────
    case 'backgroundTaskStarted': {
      const cur = state.backgroundTasks[ev.toolUseId]
      const task: BackgroundTaskState = {
        taskId: ev.taskId, description: ev.description, taskType: ev.taskType, backgrounded: ev.backgrounded,
        status: cur?.status ?? 'running', startedAt: cur?.startedAt ?? ev.ts,
        ...(cur?.endedAt != null && { endedAt: cur.endedAt }),
      }
      return { ...state, backgroundTasks: { ...state.backgroundTasks, [ev.toolUseId]: task } }
    }
    case 'backgroundTaskEnded': {
      const cur: BackgroundTaskState = state.backgroundTasks[ev.toolUseId]
        ?? { taskId: ev.taskId, description: '', taskType: 'local_bash', backgrounded: true, status: 'running' }
      if (cur.status !== 'running') return state   // the kill sequence delivers two terminal frames
      return { ...state, backgroundTasks: { ...state.backgroundTasks, [ev.toolUseId]: { ...cur, status: ev.status, endedAt: ev.ts } } }
    }
```
`replayReset` does not touch `backgroundTasks` (it is not in the reset object — verify).

- [ ] **Step 4: Run** — the two files + `(cd web && npm run typecheck)` → pass.
- [ ] **Step 5: Commit** — "feat(web): backgroundTaskStarted/Ended events + reducer state (#2635)"

---

### Task 5: Web — `Session.backgroundWork` type, `selectBackgroundWork`, summary helper

**Files:**
- Modify: `web/src/api/types.ts` (after `SubAgentSummary` ~L313; `Session` after `subAgents`)
- Modify: `web/src/session/selectors.ts` (after `selectRunningSubAgentCount`)
- Create: `web/src/features/sessions/backgroundWorkSummary.ts`
- Test: `web/src/session/__tests__/selectBackgroundWork.test.ts` (new), `web/src/features/sessions/backgroundWorkSummary.test.ts` (new)

**Interfaces:**
- Produces: `BackgroundWorkItem { toolUseId; kind: string; name; description; startedAt: string | null }`, `BackgroundWork { items; wakeup: boolean; wakeupAt: string | null }`, `Session.backgroundWork?: BackgroundWork | null`; `BackgroundWorkKind = 'shell'|'monitor'|'terminal'|'workflow'|'task'`; `BackgroundWorkRow { toolUseId; kind; description; status: BackgroundTaskStatus; startedAt?; endedAt?; live: boolean }`; `BackgroundWorkView { rows; wakeup; wakeupAt: number | null }`; `selectBackgroundWork(turns, live, snapshot?)`; `EMPTY_BACKGROUND_WORK_VIEW`; `backgroundWorkSummary(view, now): string | null`; `KIND_LABEL: Record<BackgroundWorkKind, string>`.

- [ ] **Step 1: Failing tests.** `selectBackgroundWork.test.ts`:
```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { selectBackgroundWork } from '../selectors'
import type { Turn, BackgroundTaskState } from '../reducer'
import type { BackgroundWork } from '@/api/types'

function bashTurn(toolId: string, input: Record<string, unknown>, ts = 5000, name = 'Bash'): Turn {
  return { id: 't1', role: 'assistant', blocks: [{ kind: 'tool', id: 'b', toolId, name, toolKind: 'unknown', input, ts, result: { ok: true, payload: {} } }] }
}
const live = (over: Partial<BackgroundTaskState> = {}): BackgroundTaskState =>
  ({ taskId: 'bg1', description: 'pytest -q', taskType: 'local_bash', backgrounded: true, status: 'running', startedAt: 1000, ...over })
const snap: BackgroundWork = { items: [{ toolUseId: 'tu1', kind: 'task', name: 'Bash', description: 'pytest -q', startedAt: '2026-10-02T11:00:00Z' }], wakeup: true, wakeupAt: '2026-10-02T11:10:00Z' }

describe('selectBackgroundWork', () => {
  it('renders snapshot items as running rows and parses the wakeup', () => {
    const v = selectBackgroundWork([], {}, snap)
    expect(v.rows).toEqual([{ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', startedAt: Date.parse('2026-10-02T11:00:00Z'), live: false }])
    expect(v.wakeup).toBe(true)
    expect(v.wakeupAt).toBe(Date.parse('2026-10-02T11:10:00Z'))
  })
  it('drops a snapshot item the live stream saw end', () => {
    const v = selectBackgroundWork([], { tu1: live({ status: 'completed', endedAt: 2000 }) }, snap)
    expect(v.rows).toEqual([])
  })
  it('marks a snapshot item live when the stream knows it', () => {
    expect(selectBackgroundWork([], { tu1: live() }, snap).rows[0]).toMatchObject({ live: true, startedAt: 1000 })
  })
  it('adds a live task missing from the snapshot and reads kind/description from the block', () => {
    const turns = [bashTurn('tu2', { command: 'gh pr checks 1', description: 'watch CI' }, 7000, 'Monitor')]
    const v = selectBackgroundWork(turns, { tu2: live({ description: '', backgrounded: false }) }, null)
    expect(v.rows).toEqual([{ toolUseId: 'tu2', kind: 'monitor', description: 'watch CI', status: 'running', startedAt: 1000, live: true }])
  })
  it('hides a live task that was not launched in the background', () => {
    const turns = [bashTurn('tu3', { command: 'ls' })]
    expect(selectBackgroundWork(turns, { tu3: live({ backgrounded: false }) }, null).rows).toEqual([])
  })
  it('shows a live run_in_background Bash even when the notice said not backgrounded', () => {
    const turns = [bashTurn('tu4', { command: 'sleep 9', run_in_background: true })]
    expect(selectBackgroundWork(turns, { tu4: live({ backgrounded: false, description: '' }) }, null).rows[0]).toMatchObject({ kind: 'shell', description: 'sleep 9' })
  })
  it('maps task types and snapshot kinds', () => {
    const v = selectBackgroundWork([], { w: live({ taskType: 'local_workflow' }) }, { items: [{ toolUseId: 'c', kind: 'terminal', name: 'BackgroundTerminal', description: 'npm test', startedAt: null }], wakeup: false, wakeupAt: null })
    expect(v.rows.map((r) => r.kind).sort()).toEqual(['terminal', 'workflow'])
  })
  it('sorts newest first', () => {
    const v = selectBackgroundWork([], { a: live({ startedAt: 1 }), b: live({ startedAt: 9 }) }, null)
    expect(v.rows.map((r) => r.toolUseId)).toEqual(['b', 'a'])
  })
})
```
`backgroundWorkSummary.test.ts`:
```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { backgroundWorkSummary } from './backgroundWorkSummary'
import type { BackgroundWorkRow } from '@/session/selectors'

const row = (kind: BackgroundWorkRow['kind'], status: BackgroundWorkRow['status'] = 'running'): BackgroundWorkRow =>
  ({ toolUseId: kind + status, kind, description: '', status, live: true })

describe('backgroundWorkSummary', () => {
  it('counts running rows per kind and appends the wakeup countdown', () => {
    expect(backgroundWorkSummary({ rows: [row('shell'), row('shell'), row('monitor'), row('shell', 'completed')], wakeup: true, wakeupAt: 61_000 }, 1_000))
      .toBe('2 shells · 1 monitor · wakeup in 1m 0s')
  })
  it('names an untimed wakeup as armed', () => {
    expect(backgroundWorkSummary({ rows: [], wakeup: true, wakeupAt: null }, 0)).toBe('wakeup armed')
  })
  it('is null when nothing is running', () => {
    expect(backgroundWorkSummary({ rows: [row('shell', 'stopped')], wakeup: false, wakeupAt: null }, 0)).toBeNull()
  })
})
```

- [ ] **Step 2: Run** → FAIL (missing exports).

- [ ] **Step 3: Implement.** `types.ts` after `SubAgentSummary`:
```ts
/** One open background task behind a "Waiting…" session — a backgrounded Bash shell, a
 *  Monitor, a Codex background terminal — from the server's transcript snapshot. Mirrors
 *  `background_work_snapshot` (server/sessions/background_tasks.py). Background AGENTS
 *  are excluded: `subAgents` owns them. `kind` is the server's OpenBgTask.kind
 *  ('monitor' | 'task' | 'terminal'); `name` the launching tool. */
export interface BackgroundWorkItem { toolUseId: string; kind: string; name: string; description: string; startedAt: string | null }
export interface BackgroundWork { items: BackgroundWorkItem[]; wakeup: boolean; wakeupAt: string | null }
```
In `Session`: `/** Open background work (shells / Monitors / terminals / armed wakeup) — what "Waiting…" waits on (#2635). */ backgroundWork?: BackgroundWork | null`.

`selectors.ts` (import `BackgroundTaskState, BackgroundTaskStatus` from `./reducer`, `BackgroundWork` from `@/api/types`):
```ts
// ── Background work (#2635) ───────────────────────────────────────────────────

export type BackgroundWorkKind = 'shell' | 'monitor' | 'terminal' | 'workflow' | 'task'
export interface BackgroundWorkRow {
  toolUseId: string
  kind: BackgroundWorkKind
  description: string
  status: BackgroundTaskStatus
  startedAt?: number
  endedAt?: number
  /** True when the CLI's live task notices back this row — Stop can act on it. */
  live: boolean
}
export interface BackgroundWorkView { rows: BackgroundWorkRow[]; wakeup: boolean; wakeupAt: number | null }
export const EMPTY_BACKGROUND_WORK_VIEW: BackgroundWorkView = { rows: [], wakeup: false, wakeupAt: null }

type ToolBlock = Extract<Block, { kind: 'tool' }>

function bgKind(toolName: string | undefined, taskType: string | undefined, snapshotKind: string | undefined): BackgroundWorkKind {
  if (toolName === 'Monitor' || snapshotKind === 'monitor') return 'monitor'
  if (snapshotKind === 'terminal') return 'terminal'
  if (taskType === 'local_workflow') return 'workflow'
  if (toolName === 'Bash' || taskType === 'local_bash') return 'shell'
  return 'task'
}
function bgDescription(b: ToolBlock | undefined): string {
  const i = (b?.input ?? {}) as { description?: unknown; command?: unknown }
  if (typeof i.description === 'string' && i.description) return i.description
  return typeof i.command === 'string' ? i.command : ''
}
function launchedInBackground(l: BackgroundTaskState, b: ToolBlock | undefined): boolean {
  return l.backgrounded || b?.name === 'Monitor' || (b?.input as { run_in_background?: unknown } | undefined)?.run_in_background === true
}

/**
 * Merge the server's resting snapshot (`Session.backgroundWork`) with the live task
 * state (`state.backgroundTasks`), the way selectSubAgents merges turns + threads:
 * snapshot items are running unless the stream saw them end; live entries not in the
 * snapshot show only when launched in the background (a foreground Bash is also a
 * `local_bash` task and must never appear here).
 */
export function selectBackgroundWork(turns: Turn[], live: Record<string, BackgroundTaskState>, snapshot?: BackgroundWork | null): BackgroundWorkView {
  const blocks = new Map<string, ToolBlock>()
  for (const t of turns) for (const b of t.blocks) if (b.kind === 'tool') blocks.set(b.toolId, b)
  const rows = new Map<string, BackgroundWorkRow>()
  for (const item of snapshot?.items ?? []) {
    const l = live[item.toolUseId]
    if (l && l.status !== 'running') continue
    const b = blocks.get(item.toolUseId)
    const parsed = item.startedAt ? Date.parse(item.startedAt) : NaN
    rows.set(item.toolUseId, {
      toolUseId: item.toolUseId, kind: bgKind(b?.name ?? item.name, l?.taskType, item.kind),
      description: item.description || bgDescription(b), status: 'running',
      startedAt: l?.startedAt ?? b?.ts ?? (Number.isFinite(parsed) ? parsed : undefined), live: l != null,
    })
  }
  for (const [toolUseId, l] of Object.entries(live)) {
    if (rows.has(toolUseId)) continue
    const b = blocks.get(toolUseId)
    if (!launchedInBackground(l, b)) continue
    rows.set(toolUseId, {
      toolUseId, kind: bgKind(b?.name, l.taskType, undefined), description: l.description || bgDescription(b),
      status: l.status, startedAt: l.startedAt ?? b?.ts, ...(l.endedAt != null && { endedAt: l.endedAt }), live: true,
    })
  }
  const parsedWake = snapshot?.wakeupAt ? Date.parse(snapshot.wakeupAt) : NaN
  return {
    rows: [...rows.values()].sort((a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0)),
    wakeup: snapshot?.wakeup ?? false,
    wakeupAt: Number.isFinite(parsedWake) ? parsedWake : null,
  }
}
```
(`Block` is already imported/defined in selectors.ts — check; a tool block's `ts` field name is `ts` per the sub-agent selector.) Note: `startedAt` in rows omits the key when undefined so `toEqual` matches — use conditional spreads if the test's `toEqual` complains.

`backgroundWorkSummary.ts`:
```ts
import { formatElapsed } from './elapsed'
import type { BackgroundWorkKind, BackgroundWorkView } from '@/session/selectors'

export const KIND_LABEL: Record<BackgroundWorkKind, string> = {
  shell: 'shell', monitor: 'monitor', terminal: 'terminal', workflow: 'workflow', task: 'task',
}

/** "2 shells · 1 monitor · wakeup in 1m 0s" — the one phrase the chip, the card pill
 *  and the Waiting… title all read, so they can never disagree. Null when nothing runs. */
export function backgroundWorkSummary(view: BackgroundWorkView, now: number): string | null {
  const counts = new Map<BackgroundWorkKind, number>()
  for (const r of view.rows) if (r.status === 'running') counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1)
  const parts = [...counts].map(([k, n]) => `${n} ${KIND_LABEL[k]}${n === 1 ? '' : 's'}`)
  if (view.wakeup) parts.push(view.wakeupAt != null ? `wakeup in ${formatElapsed(Math.max(0, view.wakeupAt - now))}` : 'wakeup armed')
  return parts.length ? parts.join(' · ') : null
}
```

- [ ] **Step 4: Run** the two tests + `npm run typecheck` + `npm run lint` → pass.
- [ ] **Step 5: Commit** — "feat(web): Session.backgroundWork type, selectBackgroundWork, backgroundWorkSummary (#2635)"

---

### Task 6: Web — `BackgroundWorkList`, the "Background" chip in `SessionMeta`, runtime wiring

**Files:**
- Create: `web/src/features/sessions/BackgroundWorkList.tsx`, `web/src/features/sessions/BackgroundWorkList.test.tsx`
- Modify: `web/src/features/sessions/SessionMeta.tsx` (props ~L61, `open` state, right cluster ~L323-347, popover ~L350-358)
- Modify: `web/src/session/useSessionRuntime.ts` (interface ~L70, derive ~L532, return ~L578)
- Modify: `web/src/features/sessions/SessionBody.tsx` (~L109 pass `backgroundWork={rt.backgroundWork}`)
- Test: `web/src/features/sessions/SessionMeta.test.tsx` (add 3 cases)

**Interfaces:**
- Consumes: `BackgroundWorkView`, `backgroundWorkSummary`, `KIND_LABEL`, `useStopSubAgent`, `statusTone`, `formatElapsed`, `useNow`.
- Produces: `BackgroundWorkList({ sessionId, work })`; `SessionMeta` prop `backgroundWork?: BackgroundWorkView` (default `EMPTY_BACKGROUND_WORK_VIEW`); `SessionRuntime.backgroundWork: BackgroundWorkView`.

- [ ] **Step 1: Failing tests.** `BackgroundWorkList.test.tsx` (wrap in `QueryClientProvider` — copy the wrapper from `web/src/api/hooks.stopSubAgent.test.tsx`; mock `@/api/hooks` `useStopSubAgent` to a `vi.fn()` mutation like `SubAgentPage.test.tsx` does):
```tsx
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { BackgroundWorkList } from './BackgroundWorkList'
import type { BackgroundWorkRow } from '@/session/selectors'

const mutate = vi.fn()
vi.mock('@/api/hooks', () => ({ useStopSubAgent: () => ({ mutate, isPending: false }) }))
afterEach(() => { cleanup(); mutate.mockReset() })

const row = (over: Partial<BackgroundWorkRow> = {}): BackgroundWorkRow =>
  ({ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', startedAt: Date.now() - 65_000, live: true, ...over })

describe('BackgroundWorkList', () => {
  it('renders kind, status, description and elapsed for a running shell', () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [row()], wakeup: false, wakeupAt: null }} />)
    const list = screen.getByRole('list', { name: 'Background work' })
    expect(list).toHaveTextContent('shell'); expect(list).toHaveTextContent('running'); expect(list).toHaveTextContent('pytest -q')
    expect(list).toHaveTextContent(/1m \d+s/)
  })
  it('Stop acts only on a live running row and posts the tool-use id', () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [row(), row({ toolUseId: 'tu2', live: false }), row({ toolUseId: 'tu3', status: 'completed' })], wakeup: false, wakeupAt: null }} />)
    const stops = screen.getAllByRole('button', { name: /^Stop shell/ })
    expect(stops).toHaveLength(1)
    fireEvent.click(stops[0])
    expect(mutate).toHaveBeenCalledWith({ id: 's1', toolUseId: 'tu1' })
  })
  it('shows an armed wakeup with no fire time', () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [], wakeup: true, wakeupAt: null }} />)
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent(/wakeup.*armed/)
  })
  it('counts down to a timed wakeup', () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [], wakeup: true, wakeupAt: Date.now() + 125_000 }} />)
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent(/fires in 2m \d+s/)
  })
})
```
`SessionMeta.test.tsx` additions (the file renders `SessionMeta` bare; the chip is visible without a QueryClientProvider because the list mounts only when the popover opens — for the open test, wrap in a `QueryClientProvider` and mock `useStopSubAgent` as above):
```tsx
  it('renders no Background chip when nothing is open', () => {
    render(<SessionMeta {...baseProps} />)
    expect(screen.queryByRole('button', { name: /Background work/ })).toBeNull()
  })
  it('shows a Background chip with the summary and opens the list', () => {
    render(<Wrapper><SessionMeta {...baseProps} backgroundWork={{ rows: [{ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', live: true }], wakeup: false, wakeupAt: null }} /></Wrapper>)
    const chip = screen.getByRole('button', { name: 'Background work: 1 shell' })
    expect(chip).toHaveTextContent('1 shell')
    fireEvent.click(chip)
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent('pytest -q')
  })
  it('opening the Background chip closes the sub-agent popover', () => {
    const agent: SubAgent = { toolId: 'a1', type: 'Explore', description: 'find', status: 'running' }
    render(<Wrapper><SessionMeta {...baseProps} subAgents={[agent]} backgroundWork={{ rows: [{ toolUseId: 'tu1', kind: 'monitor', description: 'ci', status: 'running', live: false }], wakeup: false, wakeupAt: null }} /></Wrapper>)
    fireEvent.click(screen.getByRole('button', { name: '1 sub-agents' }))
    expect(screen.getByRole('list', { name: 'Sub-agents' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Background work: 1 monitor' }))
    expect(screen.queryByRole('list', { name: 'Sub-agents' })).toBeNull()
    expect(screen.getByRole('list', { name: 'Background work' })).toBeInTheDocument()
  })
```
(`SessionMeta.test.tsx` wraps the popover tests in `<MemoryRouter>` (~L193); define `Wrapper` in the test file as `MemoryRouter` + `QueryClientProvider` with a fresh `QueryClient`, and mock `@/api/hooks`' `useStopSubAgent` as in the list test above.)

- [ ] **Step 2: Run** → FAIL (module not found / unknown prop).

- [ ] **Step 3: `BackgroundWorkList.tsx`**
```tsx
import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { useStopSubAgent } from '@/api/hooks'
import { formatElapsed } from './elapsed'
import { statusTone } from './subAgentStatus'
import { KIND_LABEL } from './backgroundWorkSummary'
import type { BackgroundWorkRow, BackgroundWorkView } from '@/session/selectors'

/** Row status → the shared sub-agent dot vocabulary (`completed` is `done` there). */
const DOT: Record<BackgroundWorkRow['status'], string> = { running: 'running', completed: 'done', failed: 'failed', stopped: 'stopped' }

function elapsedOf(r: BackgroundWorkRow, now: number): string | null {
  if (r.startedAt == null) return null
  if (r.status === 'running') return formatElapsed(Math.max(0, now - r.startedAt))
  return r.endedAt == null ? null : formatElapsed(Math.max(0, r.endedAt - r.startedAt))
}

/** The "Background" chip's list (#2635): one row per open shell / Monitor / terminal /
 *  workflow with kind, status, elapsed and description, Stop on a live running row
 *  (the same `stop_task` route the sub-agent Stop uses), and the armed wakeup. Rows
 *  are not links — a shell has no transcript to drill into. */
export function BackgroundWorkList({ sessionId, work }: { sessionId: string; work: BackgroundWorkView }) {
  const ticking = work.rows.some((r) => r.status === 'running') || work.wakeupAt != null
  const now = useNow(1000, ticking)
  const stop = useStopSubAgent()
  return (
    <ul className="flex flex-col gap-1 py-1" aria-label="Background work">
      {work.wakeup && (
        <li className="flex items-center gap-2 px-1 py-1 text-xs min-h-target">
          <span className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone('running'))} aria-hidden="true" />
          <span className="font-medium text-ink">wakeup</span>
          <span className="text-ink-muted">
            {work.wakeupAt != null ? `fires in ${formatElapsed(Math.max(0, work.wakeupAt - now))}` : 'armed'}
          </span>
        </li>
      )}
      {work.rows.map((r) => {
        const elapsed = elapsedOf(r, now)
        return (
          <li key={r.toolUseId} className="flex items-start gap-2 px-1 py-1 text-xs min-h-target">
            <span className={cn('mt-1 h-2 w-2 flex-shrink-0 rounded-full', statusTone(DOT[r.status]))} aria-hidden="true" />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-1.5">
                <span className="font-medium text-ink">{KIND_LABEL[r.kind]}</span>
                <span className="text-2xs uppercase tracking-wide text-ink-muted">{r.status}</span>
                {elapsed && <span className="ml-auto flex-shrink-0 tabular-nums text-ink-muted">{elapsed}</span>}
              </span>
              {r.description && <span className="text-ink-muted line-clamp-2 break-all">{r.description}</span>}
            </span>
            {r.live && r.status === 'running' && (
              <button
                type="button"
                onClick={() => stop.mutate({ id: sessionId, toolUseId: r.toolUseId })}
                disabled={stop.isPending}
                aria-label={`Stop ${KIND_LABEL[r.kind]}${r.description ? `: ${r.description}` : ''}`}
                className={cn('flex-shrink-0 rounded px-1.5 py-0.5 text-2xs text-ink-muted min-h-7',
                  'hover:bg-edge focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent')}
              >
                Stop
              </button>
            )}
          </li>
        )
      })}
    </ul>
  )
}
```

- [ ] **Step 4: `SessionMeta.tsx`.** Props: `/** Open background work (shells / Monitors / wakeup) for the "Background" chip (#2635). */ backgroundWork?: BackgroundWorkView` with default `EMPTY_BACKGROUND_WORK_VIEW` in the destructure. Replace `const [open, setOpen] = useState(false)` with `const [panel, setPanel] = useState<'agents' | 'background' | null>(null)`; the agents button uses `onClick={() => setPanel((p) => (p === 'agents' ? null : 'agents'))}`, `aria-expanded={panel === 'agents'}`; its popover condition `panel === 'agents' && agentCount > 0`. Add `const now = useNow(1000, backgroundWork.wakeupAt != null)` and `const bgSummary = backgroundWorkSummary(backgroundWork, now)`. Extend the right-cluster guard with `|| bgSummary` and add after the agents button:
```tsx
            {bgSummary && (
              <button
                type="button"
                onClick={() => setPanel((p) => (p === 'background' ? null : 'background'))}
                aria-expanded={panel === 'background'}
                aria-label={`Background work: ${bgSummary}`}
                className={cn('inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md min-h-7',
                  'hover:bg-edge active:bg-edge-strong',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent')}
              >
                <span className={cn('h-2 w-2 rounded-full', statusTone('running'))} aria-hidden="true" />
                <span className="text-ink">{bgSummary}</span>
              </button>
            )}
```
and a second popover block (same classes as the sub-agent one) rendering `<BackgroundWorkList sessionId={sessionId} work={backgroundWork} />` when `panel === 'background' && bgSummary`.

- [ ] **Step 5: Runtime + body.** `useSessionRuntime.ts`: interface `/** Open background work — the shells / Monitors / wakeup a "Waiting…" session waits on (#2635). Live events win; the record snapshot covers reload. */ backgroundWork: BackgroundWorkView`; derive next to `subAgents`: `const backgroundWork = useMemo(() => selectBackgroundWork(viewState.turns, viewState.backgroundTasks, sessionRecord?.backgroundWork), [viewState.turns, viewState.backgroundTasks, sessionRecord?.backgroundWork])` (follow the file's existing memo style — if `subAgents` is not memoised, don't memoise this either); add to the returned object. `SessionBody.tsx`: pass `backgroundWork={rt.backgroundWork}` to `SessionMeta`. Then grep `web/src` for other `SessionRuntime` fakes/builders in tests (`rg "subAgents: \[\]" web/src --glob '*.test.tsx'`) and add `backgroundWork: EMPTY_BACKGROUND_WORK_VIEW` where a full runtime object is built, so typecheck passes.

- [ ] **Step 6: Run** — `(cd web && npx vitest run src/features/sessions/BackgroundWorkList.test.tsx src/features/sessions/SessionMeta.test.tsx src/features/sessions/SessionMeta.fold.test.tsx src/features/sessions/SessionMeta.switchers.test.tsx && npm run typecheck && npm run lint)` → pass.
- [ ] **Step 7: Commit** — "feat(web): Background chip + list beside the sub-agent chip (#2635)"

---

### Task 7: Web — session card: count pill, read-only rows, Waiting… title

**Files:**
- Modify: `web/src/features/sessions/list/SessionCard.tsx` (~L102 derive; trailing ~L413; rows ~L946; pill ~L612)
- Create: `web/src/features/sessions/list/BackgroundWorkRows.tsx`
- Test: `web/src/features/sessions/list/SessionCard.backgroundWork.test.tsx` (new)

**Interfaces:**
- Consumes: `selectBackgroundWork([], {}, session.backgroundWork)`, `backgroundWorkSummary`, `KIND_LABEL`, `statusTone`.
- Produces: `BackgroundWorkRows({ work })` (no navigation, no Stop).

- [ ] **Step 1: Failing test** (copy `makeSession`/`renderCard` from `SessionCard.children.test.tsx`):
```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return { id: 's1', name: 'My session', accountName: 'acct', status: 'waiting_external',
    createdAt: new Date().toISOString(), prs: [], ...over } as Session
}
function renderCard(session: Session) {
  return render(<ViewportProvider kiosk={null}><MemoryRouter>
    <SessionCard session={session} account={undefined} onOpen={() => {}} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />
  </MemoryRouter></ViewportProvider>)
}
const work = { items: [{ toolUseId: 'tu1', kind: 'task', name: 'Bash', description: 'pytest -q', startedAt: null }], wakeup: true, wakeupAt: null }

describe('SessionCard background work (#2635)', () => {
  it('shows the summary pill collapsed and the rows expanded', () => {
    renderCard(makeSession({ backgroundWork: work }))
    expect(screen.getByTestId('card-background-pill')).toHaveTextContent('1 shell · wakeup armed')
    expect(screen.queryByRole('group', { name: 'Background work' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show background work' }))
    expect(screen.getByRole('group', { name: 'Background work' })).toHaveTextContent('pytest -q')
  })
  it('titles the Waiting… pill with what it waits on', () => {
    renderCard(makeSession({ backgroundWork: work }))
    expect(screen.getByLabelText('Status: Waiting…')).toHaveAttribute('title', 'Waiting on 1 shell · wakeup armed')
  })
  it('renders nothing extra when there is no background work', () => {
    renderCard(makeSession({ backgroundWork: { items: [], wakeup: false, wakeupAt: null } }))
    expect(screen.queryByTestId('card-background-pill')).toBeNull()
    expect(screen.getByLabelText('Status: Waiting…')).not.toHaveAttribute('title')
  })
})
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: `BackgroundWorkRows.tsx`**
```tsx
import { cn } from '@/lib/utils'
import { statusTone } from '@/features/sessions/subAgentStatus'
import { KIND_LABEL } from '@/features/sessions/backgroundWorkSummary'
import type { BackgroundWorkView } from '@/session/selectors'

/** Read-only background-work rows for a session card (#2635): kind + description, and
 *  the armed wakeup. Stop lives in the session view's chip, not on the card. */
export function BackgroundWorkRows({ work }: { work: BackgroundWorkView }) {
  if (work.rows.length === 0 && !work.wakeup) return null
  return (
    <div role="group" aria-label="Background work" className="mt-1 border-t border-edge">
      {work.wakeup && (
        <div className="flex items-center gap-2 py-1.5 pl-3 pr-2 text-xs text-ink-muted min-h-9">
          <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone('running'))} />
          <span className="font-medium text-ink">wakeup</span><span>armed</span>
        </div>
      )}
      {work.rows.map((r) => (
        <div key={r.toolUseId} className="flex items-center gap-2 py-1.5 pl-3 pr-2 text-xs text-ink-muted min-h-9">
          <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone('running'))} />
          <span className="font-medium text-ink">{KIND_LABEL[r.kind]}</span>
          {r.description && <span className="truncate opacity-80">{r.description}</span>}
        </div>
      ))}
    </div>
  )
}
```
`SessionCard.tsx`: next to `const subAgents = session.subAgents ?? []` add
```ts
  const backgroundWork = selectBackgroundWork([], {}, session.backgroundWork)
  const bgSummary = backgroundWorkSummary(backgroundWork, Date.now())
```
(The card re-renders on every list poll, so a static `Date.now()` is enough for the "wakeup in …" phrase; do not add a ticker.) In `trailing`, after the agents count span: `{bgSummary && !expanded && <span data-testid="card-background-pill" className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-info-wash text-info">{bgSummary}</span>}`. After `SubAgentRows`: `{bgSummary && expanded && <BackgroundWorkRows work={backgroundWork} />}`. On the status `<span aria-label={`Status: …`}>` add `title={presentation.tier === 'waiting' && bgSummary ? `Waiting on ${bgSummary}` : undefined}`. The chevron is gated by `hasNested = hasSubAgents || hasChildren` (~L107) with `nestedNoun` naming what it holds: add `const hasBackground = bgSummary != null`, make `hasNested = hasSubAgents || hasChildren || hasBackground`, and extend `nestedNoun` so a card with only background work reads `Show background work` (build the noun from the present parts joined with ' and ': `['sub-agents', 'child sessions', 'background work']` filtered by `[hasSubAgents, hasChildren, hasBackground]` — the existing two-part strings must stay byte-identical for the existing tests).

- [ ] **Step 4: Run** the new test + the other `SessionCard.*.test.tsx` + typecheck + lint → pass.
- [ ] **Step 5: Commit** — "feat(web): session card shows what Waiting… waits on (#2635)"

---

### Task 8: Verification, docs touch, PR

- [ ] **Step 1: Full web suite** — `(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/bgwork-web.log; echo "exit=${PIPESTATUS[0]}" ) ` — must be 0 (vitest's exit code, not tee's).
- [ ] **Step 2: Server gate, all four steps** — `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, then the full `pytest server/tests/ -n auto --dist loadfile --timeout=300` in the background with the test-slot acquire block from `core.md` in the same shell, tee'd to `/tmp/bgwork-server.log`. Known host-only flakes (UDS entrypoint, reauth timing) must pass alone.
- [ ] **Step 3: e2e grep** — `rg "sub-agents|agents'" web/e2e` for specs asserting the right-cluster buttons; run any that touch `SessionMeta`.
- [ ] **Step 4: Docs** — the spec (`docs/superpowers/specs/2026-10-02-background-work-visibility-design.md`) is the feature record; append a "Shipped" line to it naming the PR number. No other docs change.
- [ ] **Step 5: Branch/PR** — `git fetch && git rebase origin/master` (branch already off `origin/master`), push, `gh pr create --body-file /tmp/bgwork-pr.md` with `Fixes #2635`, the verified-CLI facts, and screenshots if a headless harness is handy; `gh pr merge <#> --squash --auto`.
