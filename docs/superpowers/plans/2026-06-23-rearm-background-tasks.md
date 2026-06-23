# Re-arm Background Tasks Across Daemon Redeploys — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a deploy restarts the daemon, automatically relaunch the background tasks (harness Monitors and backgrounded Bash/Task commands) that were live at drain time, so they survive redeploys instead of being silently reaped by `KillMode=control-group`.

**Architecture:** A pure scanner reads each session's transcript `.jsonl` (the durable source of truth for both launches and terminations). At `begin_drain` — the daemon's own "about to restart" signal — the orchestrator snapshots each live session's *open* background tasks into a new nullable `Session.pending_rearm` JSON column; `cancel_drain` clears it. After restart, the daemon calls `rearm_background_tasks()`, which eagerly resumes each session with a pending snapshot and injects one batched re-arm turn, then clears the snapshot. Side-effectful (non-Monitor) tasks get a verify-before-acting guard in the injected prompt.

**Tech Stack:** Python 3 · asyncio · SQLAlchemy 2.x async (SQLite dev / Postgres prod) · pytest. Backend lives under `server/`.

## Global Constraints

- New DB columns MUST be **nullable** with a default (dev SQLite `init_db` reconcile backfills nullable columns; it never ALTERs a NOT NULL column). Mirror the existing `sub_agents` JSON column.
- Single source of truth: do NOT add a live observer or a table that mirrors transcript state. Scan the transcript; reuse `server/sessions/replay.py` parsing conventions (skip blank/`isSidechain`, tolerate `message.content` as str or list).
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit.
- Run backend tests from the prebuilt env with the worktree on the path:
  `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/<file> -v`
  (a fresh worktree can't `uv sync`; the venv's editable `ccswitch` resolves to a stale checkout — see CLAUDE.md "Dev / testing" and memory `ccswitch-worktree-backend-pythonpath`).
- Backend CI gate runs `ruff` + `mypy` + `pytest`; run all three locally before declaring a task done.
- Re-arm concurrency cap env var: `CCSWITCH_REARM_CONCURRENCY` (default `3`).

---

### Task 1: Transcript scanner + re-arm prompt builder

**Files:**
- Create: `server/sessions/background_tasks.py`
- Test: `server/tests/test_background_tasks_scan.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces:
  - `@dataclass class OpenBgTask: tool_use_id: str; kind: str; name: str; input: dict; description: str` with `to_dict() -> dict` and `from_dict(d: dict) -> OpenBgTask` (classmethod).
  - `scan_open_background_tasks(jsonl_path: str) -> list[OpenBgTask]` — open tasks, newest-first, deduped by `tool_use_id`.
  - `build_rearm_prompt(tasks: list[OpenBgTask]) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_background_tasks_scan.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_background_tasks_scan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.background_tasks'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/background_tasks.py
"""Scan a Claude transcript for *open* background tasks (Monitors / backgrounded
Bash|Task) and build the turn that relaunches them after a deploy restart.

Pure + side-effect-free: the transcript .jsonl is the durable source of truth for
both launches (assistant tool_use) and terminations (task-notification), so no live
observer or mirror table is needed (constitution Principle VI)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_TUID = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
_STATUS = re.compile(r"<status>(.*?)</status>")
# completed/failed = the task finished on its own; killed = interrupted (we re-arm it).
_TERMINAL = {"completed", "failed"}

_GUARD = ("This task was interrupted by a server restart and may have partially "
          "completed. Before redoing work, check current state (e.g. `git status`, "
          "what already exists) and continue from where it left off rather than "
          "restarting blindly.")


@dataclass
class OpenBgTask:
    tool_use_id: str
    kind: str          # "monitor" | "task"
    name: str          # originating tool name (Monitor / Bash / Task)
    input: dict        # verbatim tool_use input — replayed to relaunch faithfully
    description: str

    def to_dict(self) -> dict:
        return {"tool_use_id": self.tool_use_id, "kind": self.kind,
                "name": self.name, "input": self.input, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> "OpenBgTask":
        return cls(tool_use_id=d.get("tool_use_id", ""), kind=d.get("kind", "task"),
                   name=d.get("name", ""), input=d.get("input") or {},
                   description=d.get("description", ""))


def _is_background_launch(name: str, inp: dict) -> bool:
    return name == "Monitor" or bool(inp.get("run_in_background"))


def scan_open_background_tasks(jsonl_path: str) -> list[OpenBgTask]:
    launches: dict[str, OpenBgTask] = {}
    terminated: set[str] = set()
    try:
        f = open(jsonl_path, errors="ignore")
    except OSError:
        return []
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict) or o.get("isSidechain"):
                continue
            if o.get("type") == "assistant":
                content = (o.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name") or ""
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        tid = b.get("id")
                        if tid and _is_background_launch(name, inp):
                            launches[tid] = OpenBgTask(
                                tool_use_id=tid,
                                kind="monitor" if name == "Monitor" else "task",
                                name=name, input=inp,
                                description=str(inp.get("description") or ""))
            # Terminations: any entry whose content carries a task-notification with a
            # completed/failed status (queue-operation enqueue or the user delivery).
            content = o.get("content")
            if isinstance(content, str) and "<task-notification>" in content:
                ms, mt = _STATUS.search(content), _TUID.search(content)
                if ms and mt and ms.group(1) in _TERMINAL:
                    terminated.add(mt.group(1))
    open_tasks = [t for tid, t in launches.items() if tid not in terminated]
    open_tasks.reverse()   # newest-first (dict preserves file order)
    return open_tasks


def build_rearm_prompt(tasks: list[OpenBgTask]) -> str:
    lines = ["The daemon was restarted (deploy) and these background tasks were "
             "stopped. Relaunch each one with exactly the parameters shown."]
    if any(t.kind != "monitor" for t in tasks):
        lines.append(_GUARD)
    for t in tasks:
        label = t.description or t.name
        lines.append(f"- [{t.kind}] {label}: relaunch the {t.name} tool with input "
                     f"{json.dumps(t.input)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_background_tasks_scan.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + commit**

```bash
<venv>/bin/python -m ruff check server/sessions/background_tasks.py server/tests/test_background_tasks_scan.py
<venv>/bin/python -m mypy server/sessions/background_tasks.py
git add server/sessions/background_tasks.py server/tests/test_background_tasks_scan.py
git commit -m "feat(rearm): transcript scanner for open background tasks + prompt builder"
```

---

### Task 2: `pending_rearm` DB column

**Files:**
- Modify: `server/db/models.py` (the `Session` model, after the `sub_agents` column ~`:62`)
- Test: `server/tests/test_pending_rearm_column.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Session.pending_rearm: list | None` — JSON list of `OpenBgTask.to_dict()` dicts, or `NULL`. Read/written by Tasks 3–5.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pending_rearm_column.py
import asyncio
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession


def test_pending_rearm_roundtrips_and_defaults_null(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path)))
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None            # defaults NULL
            row.pending_rearm = [{"tool_use_id": "x", "kind": "monitor",
                                  "name": "Monitor", "input": {"command": "z"},
                                  "description": "d"}]
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm[0]["tool_use_id"] == "x"

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_pending_rearm_column.py -v`
Expected: FAIL — `AttributeError: ... 'pending_rearm'` / column does not exist.

- [ ] **Step 3: Write minimal implementation**

In `server/db/models.py`, inside `class Session`, immediately after the `sub_agents` column:

```python
    # Open background tasks (Monitors / backgrounded Bash|Task) snapshotted at deploy
    # drain so the daemon can relaunch them after the restart that reaps them. JSON list
    # of {tool_use_id, kind, name, input, description}; NULL = nothing pending. Nullable
    # so init_db backfills it on the dev SQLite DB without a migration.
    pending_rearm: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
```

- [ ] **Step 4: Run test + the schema-drift guard**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_pending_rearm_column.py server/tests/test_db_schema_reconcile.py -v`
Expected: PASS (new column round-trips; reconcile/drift test still green).

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_pending_rearm_column.py
git commit -m "feat(rearm): add nullable Session.pending_rearm snapshot column"
```

---

### Task 3: Snapshot open tasks at drain; clear on cancel

**Files:**
- Modify: `server/sessions/orchestrator.py` — `begin_drain()` (~`:780`), `cancel_drain()` (~`:795`); add a private `_snapshot_open_tasks()` and `_clear_rearm_snapshots()`.
- Test: `server/tests/test_rearm_drain_snapshot.py`

**Interfaces:**
- Consumes: `scan_open_background_tasks` (Task 1), `Session.pending_rearm` (Task 2), existing `self._drivers`, `self.transcript_path()`.
- Produces: `begin_drain()` writes `pending_rearm` for live-driver sessions with open tasks; `cancel_drain()` nulls every `pending_rearm`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_rearm_drain_snapshot.py
import asyncio, json
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _seed_transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "uuid": "u",
        "message": {"content": [{"type": "tool_use", "id": "toolu_mon", "name": "Monitor",
                                 "input": {"description": "Watch PR", "command": "z"}}]}}) + "\n")
    return str(p)


def test_begin_drain_snapshots_open_tasks_then_cancel_clears(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path)))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        orch._drivers["s1"] = object()                       # pretend a live process
        orch.transcript_path = lambda sid: _seed_transcript(tmp_path)  # type: ignore

        await orch.begin_drain(selfclear_sec=600)
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm and row.pending_rearm[0]["tool_use_id"] == "toolu_mon"

        orch.cancel_drain()
        await asyncio.sleep(0)                               # let the clear task run
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_drain_snapshot.py -v`
Expected: FAIL — `pending_rearm` stays `None` after `begin_drain` (no snapshot logic yet).

- [ ] **Step 3: Write minimal implementation**

Add an import near the top of `orchestrator.py` (with the other `from .` imports):

```python
from .background_tasks import scan_open_background_tasks
```

Add two helpers to `SessionOrchestrator` (place them just above `drain_status`):

```python
    async def _snapshot_open_tasks(self) -> None:
        """At drain, persist each live session's open background tasks so the post-restart
        daemon can relaunch them. Only sessions with a live driver can have live tasks."""
        from sqlalchemy import update as sa_update
        session_ids = list(self._drivers.keys())
        if not session_ids:
            return

        async def _scan(sid: str):
            path = self.transcript_path(sid)
            if not path:
                return sid, []
            tasks = await asyncio.to_thread(scan_open_background_tasks, path)
            return sid, [t.to_dict() for t in tasks]

        results = await asyncio.gather(*(_scan(s) for s in session_ids),
                                       return_exceptions=True)
        n = 0
        async with db_session() as db:
            for r in results:
                if isinstance(r, Exception):
                    log.warning("rearm snapshot: scan failed", exc_info=r)
                    continue
                sid, snap = r
                if snap:
                    await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                                     .values(pending_rearm=snap))
                    n += len(snap)
        if n:
            log.info("rearm snapshot: %d open task(s) across %d session(s)",
                     n, len(session_ids))

    async def _clear_rearm_snapshots(self) -> None:
        from sqlalchemy import update as sa_update
        async with db_session() as db:
            await db.execute(sa_update(DbSession).values(pending_rearm=None))
```

In `begin_drain`, snapshot on the transition into draining — add the `await self._snapshot_open_tasks()` call inside the `if not self._draining:` block, after `self._draining = True` / `self._drain_started_at = ...`:

```python
        if not self._draining:
            self._draining = True
            self._drain_started_at = time.monotonic()
            try:
                await self._snapshot_open_tasks()
            except Exception:
                log.warning("rearm snapshot failed at drain", exc_info=True)
            self._publish_event({"type": "updatePending"})
            ...   # (existing self-clear timer arming unchanged)
```

In `cancel_drain`, clear snapshots when leaving draining — inside the `if self._draining:` block, schedule the async clear (cancel_drain is sync):

```python
        if self._draining:
            self._draining = False
            self._publish_event({"type": "updateCancelled"})
            asyncio.create_task(self._clear_rearm_snapshots())
            ...   # (existing timer cancel unchanged)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_drain_snapshot.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
<venv>/bin/python -m ruff check server/sessions/orchestrator.py server/tests/test_rearm_drain_snapshot.py
<venv>/bin/python -m mypy server/sessions/orchestrator.py
git add server/sessions/orchestrator.py server/tests/test_rearm_drain_snapshot.py
git commit -m "feat(rearm): snapshot open background tasks at drain, clear on cancel"
```

---

### Task 4: `rearm_background_tasks()` + daemon wiring

**Files:**
- Modify: `server/sessions/orchestrator.py` — add `self._rearmed: set[str] = set()` in `__init__` (next to `self._records`, ~`:163`); add `rearm_background_tasks()`.
- Modify: `server/ipc/serve.py` — call it after `rehydrate()` in `daemon_lifespan` (~`:39`).
- Test: `server/tests/test_rearm_dispatch.py`

**Interfaces:**
- Consumes: `Session.pending_rearm`, `build_rearm_prompt` + `OpenBgTask.from_dict` (Task 1), `ensure_driver`, `send_prompt`.
- Produces: `rearm_background_tasks() -> int` — number of sessions re-armed. On success a session is added to `self._rearmed` and its `pending_rearm` cleared; on failure the snapshot is left for retry.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_rearm_dispatch.py
import asyncio
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_SNAP = [{"tool_use_id": "toolu_mon", "kind": "monitor", "name": "Monitor",
          "input": {"description": "Watch PR", "command": "z"}, "description": "Watch PR"}]


def test_rearm_dispatches_one_turn_and_clears(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             pending_rearm=_SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        sent = {}
        class _D:  # stand-in driver
            async def send_prompt(self, text, images=None): sent["text"] = text
        async def _ensure(sid, identity="system"):
            orch._drivers[sid] = _D(); return orch._drivers[sid]
        orch.ensure_driver = _ensure  # type: ignore

        n = await orch.rearm_background_tasks()
        assert n == 1
        assert "Watch PR" in sent["text"] and '"command": "z"' in sent["text"]
        assert "s1" in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None

        # idempotent: a second pass does nothing (cleared + in _rearmed)
        assert await orch.rearm_background_tasks() == 0

    asyncio.run(run())


def test_rearm_failure_retains_snapshot(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r2.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             pending_rearm=_SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        async def _boom(sid, identity="system"): raise RuntimeError("no account")
        orch.ensure_driver = _boom  # type: ignore

        assert await orch.rearm_background_tasks() == 0
        assert "s1" not in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm == _SNAP            # retained for retry

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_dispatch.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'rearm_background_tasks'` (and `_rearmed`).

- [ ] **Step 3: Write minimal implementation**

In `__init__`, after `self._records: dict[str, SessionRecord] = {}`:

```python
        # Sessions whose background tasks have been re-armed this process (dedup the
        # auto pass against retry-on-attach). Cleared by a fresh process start.
        self._rearmed: set[str] = set()
```

Add the method (place it just after `rehydrate`). Extend the import added in Task 3:

```python
from .background_tasks import scan_open_background_tasks, build_rearm_prompt, OpenBgTask
```

```python
    async def rearm_background_tasks(self) -> int:
        """Relaunch background tasks snapshotted at the last drain. Called once after
        rehydrate() on daemon start; safe to call again (cleared snapshots + _rearmed
        make it idempotent). Returns the count of sessions re-armed."""
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            rows = (await db.execute(sa_select(DbSession).where(
                DbSession.pending_rearm.isnot(None)))).scalars().all()
        pending = [(r.id, r.pending_rearm) for r in rows
                   if r.pending_rearm and r.id not in self._rearmed]
        if not pending:
            return 0
        sem = asyncio.Semaphore(int(os.environ.get("CCSWITCH_REARM_CONCURRENCY", "3")))

        async def _one(sid: str, snap: list) -> bool:
            async with sem:
                try:
                    return await self._rearm_session(sid, snap)
                except Exception:
                    log.warning("rearm: session %s failed; snapshot retained", sid,
                                exc_info=True)
                    return False

        results = await asyncio.gather(*(_one(sid, snap) for sid, snap in pending))
        n = sum(1 for ok in results if ok)
        log.info("rearm: re-armed %d/%d session(s)", n, len(pending))
        return n

    async def _rearm_session(self, sid: str, snap: list) -> bool:
        from sqlalchemy import update as sa_update
        tasks = [OpenBgTask.from_dict(d) for d in snap if isinstance(d, dict)]
        if not tasks:
            return False
        driver = await self.ensure_driver(sid)
        if driver is None:
            return False
        await driver.send_prompt(build_rearm_prompt(tasks))
        self._rearmed.add(sid)
        async with db_session() as db:
            await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                             .values(pending_rearm=None))
        return True
```

In `server/ipc/serve.py` `daemon_lifespan`, after the `rehydrate()` try/except block and before `await orchestrator.start()`:

```python
    try:
        await orchestrator.rearm_background_tasks()
    except Exception:
        log.error("daemon rearm_background_tasks() failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_dispatch.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
<venv>/bin/python -m ruff check server/sessions/orchestrator.py server/ipc/serve.py server/tests/test_rearm_dispatch.py
<venv>/bin/python -m mypy server/sessions/orchestrator.py server/ipc/serve.py
git add server/sessions/orchestrator.py server/ipc/serve.py server/tests/test_rearm_dispatch.py
git commit -m "feat(rearm): auto-relaunch snapshotted background tasks on daemon start"
```

---

### Task 5: Retry-on-attach in `ensure_driver`

**Files:**
- Modify: `server/sessions/orchestrator.py` — `ensure_driver()` (after the driver is built/registered, ~`:961`).
- Test: `server/tests/test_rearm_on_attach.py`

**Interfaces:**
- Consumes: `Session.pending_rearm`, `self._rearmed`, `_rearm_session` (Task 4).
- Produces: when a detached session whose row still has a non-empty `pending_rearm` (not in `_rearmed`) is attached, `ensure_driver` dispatches its re-arm before returning the driver.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_rearm_on_attach.py
import asyncio
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_SNAP = [{"tool_use_id": "toolu_mon", "kind": "monitor", "name": "Monitor",
          "input": {"command": "z"}, "description": "Watch PR"}]


def test_attach_triggers_pending_rearm(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             claude_session_id="c1", pending_rearm=_SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        sent = {}
        class _D:
            claude_session_id = "c1"
            async def send_prompt(self, text, images=None): sent["text"] = text
            async def start(self): pass
        # short-circuit the heavy build path: return a fake driver directly
        orig_ensure = orch.ensure_driver
        async def _ensure(sid, identity="system"):
            if sid in orch._drivers:
                return orch._drivers[sid]
            orch._drivers[sid] = _D(); orch._records[sid].detached = False
            await orch._maybe_rearm_on_attach(sid)     # the hook under test
            return orch._drivers[sid]
        orch.ensure_driver = _ensure  # type: ignore

        d = await orch.ensure_driver("s1")
        assert "Watch PR" in sent["text"]
        assert "s1" in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None

    asyncio.run(run())
```

> Note: the test calls a small extracted hook `_maybe_rearm_on_attach(sid)`. Implement that hook and call it from the real `ensure_driver` after the driver is registered (`self._drivers[session_id] = driver; rec.detached = False`).

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_on_attach.py -v`
Expected: FAIL — `AttributeError: ... '_maybe_rearm_on_attach'`.

- [ ] **Step 3: Write minimal implementation**

Add the hook to `SessionOrchestrator` (next to `_rearm_session`):

```python
    async def _maybe_rearm_on_attach(self, session_id: str) -> None:
        """Retry a pending re-arm when a session is attached (covers sessions the auto
        pass skipped or failed). Idempotent via _rearmed; never raises into attach."""
        if session_id in self._rearmed:
            return
        from sqlalchemy import select as sa_select
        try:
            async with db_session() as db:
                row = (await db.execute(sa_select(DbSession).where(
                    DbSession.id == session_id))).scalar_one_or_none()
            snap = row.pending_rearm if row else None
            if snap:
                await self._rearm_session(session_id, snap)
        except Exception:
            log.warning("rearm-on-attach failed session=%s", session_id, exc_info=True)
```

In the real `ensure_driver`, immediately after `self._drivers[session_id] = driver` / `rec.detached = False` (inside the lock, ~`:962`), before `return driver`:

```python
            await self._maybe_rearm_on_attach(session_id)
```

> `_rearm_session` calls `ensure_driver(sid)`, which will now hit the fast-path (driver already registered) and return it — no recursion, since the driver is in `self._drivers` before the hook runs.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_on_attach.py -v`
Expected: PASS.

- [ ] **Step 5: Full backend gate + commit**

```bash
<venv>/bin/python -m ruff check server/
<venv>/bin/python -m mypy server/sessions/orchestrator.py
PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/ -q
git add server/sessions/orchestrator.py server/tests/test_rearm_on_attach.py
git commit -m "feat(rearm): retry pending re-arm on session attach"
```

Expected: full suite green (modulo the 2 known env-only failures `remote_mode_wiring` / `x11` noted in memory `ccswitch-worktree-backend-pythonpath`).

---

## Self-Review

**Spec coverage:**
- Scanner / ground-truth shapes (spec §1, "Ground truth") → Task 1.
- `pending_rearm` nullable column (spec §2) → Task 2.
- Snapshot at `begin_drain`, clear on `cancel_drain`/self-clear (spec §3) → Task 3 (`cancel_drain` is called by `_drain_selfclear`, so the self-clear path is covered transitively).
- Auto re-arm on restart, eager `ensure_driver`, one batched turn, concurrency cap, logging, verify-guard (spec §4, §5, §8) → Task 4 + Task 1's `build_rearm_prompt`.
- Idempotency/dedup (spec §6) → `pending_rearm` clear + `_rearmed` (Tasks 4–5).
- Failure handling + retry-on-attach (spec §7) → Task 4 (retain on failure) + Task 5.

**Placeholder scan:** none — every step carries full code/commands.

**Type consistency:** `OpenBgTask` (Task 1) used verbatim in Tasks 3–5; `scan_open_background_tasks`/`build_rearm_prompt`/`from_dict`/`to_dict` names match across tasks; `pending_rearm` column name consistent; `_rearmed`, `_rearm_session`, `_maybe_rearm_on_attach`, `_snapshot_open_tasks`, `_clear_rearm_snapshots`, `rearm_background_tasks` names consistent across Tasks 3–5.

**Notes for the implementer:** Replace `<venv>` with the main checkout's `.venv/bin/python` and run from the worktree root (see Global Constraints). The Task 4/5 tests monkeypatch `ensure_driver` to avoid the heavy SDK build path; the real wiring is exercised by the full suite in Task 5's gate.
