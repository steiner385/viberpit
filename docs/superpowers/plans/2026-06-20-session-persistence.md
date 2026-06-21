# Session Persistence Across Restarts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sessions persist across server restarts and are fully resumable until explicitly deleted. After a restart, every non-ended session reappears in the list with its full conversation history, and the user can keep chatting — the session reattaches to the same underlying Claude conversation via `resume=<claude_session_id>` on reopen. Resume is **lazy**: no Claude process is re-spawned on boot; a driver is created+started only when the user attaches to (reopens) a rehydrated session.

**Architecture:** The DB `sessions` table is today **write-only** — rows are inserted on create and mutated on close/rename/rotate, but nothing ever reads them back, and `SessionOrchestrator` keeps the only live view in memory (`_records`, `_drivers`). This plan turns the table into the durable source of truth for *session identity + resume coordinates* by (1) persisting the Claude session id the moment the driver captures it, (2) adding `orchestrator.rehydrate()` to rebuild **detached** `SessionRecord`s (no driver) from non-ended rows at startup, (3) lazily constructing+starting a resumed driver when a detached session is first attached, and (4) deriving `transcript_path()` from the DB row when no live driver exists. The in-memory `SessionRecord` stays the runtime view; the DB row is the cold-start seed. No new tables, no schema change to `sessions` (every needed column already exists).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x async + aiosqlite, `claude-agent-sdk` (`ClaudeSDKClient` via `real_sdk_factory`); pytest for server tests with a fake SDK client (`server/tests/fakes/fake_sdk.py` + `fake_factory.py`).

## Global Constraints

- **No new code consumes account quota in tests.** All backend tests use the fake SDK client factory (`server.tests.fakes.fake_factory.fake_sdk_factory`); real `claude` is never spawned in CI.
- **Lazy resume only.** Never iterate rehydrated rows and start drivers at boot. A driver for a detached session is created **on attach** and nowhere else. Rehydration must complete in O(rows) DB work with zero subprocess spawns.
- **File-backed DB required for persistence to do anything.** Production runs with `CCSWITCH_DB_URL=sqlite+aiosqlite:////home/tony/.claude-accounts/ccswitch.db` (file-backed), but `server/app.py` defaults to `sqlite+aiosqlite:///:memory:` when the env var is unset. An in-memory DB is wiped on restart, so rehydration is a no-op there by design. **Every test that exercises rehydration-across-restart MUST use a file-backed SQLite URL** (`tmp_path / "ccswitch.db"`) or a single shared engine reused across two `init_db()` calls — an `:memory:` engine cannot simulate a restart because the second `init_db()` opens a fresh empty database.
- **Rehydration is additive, never destructive.** It must not insert, update, or delete any row, and must skip rows whose id is already present in `_records` (so a re-run, or a rehydrate after sessions already exist, is idempotent).
- **`claude_session_id` may legitimately be NULL.** A session that was created but never received a first assistant response before the restart has `claude_session_id IS NULL`. It cannot be resumed (no transcript, no resume id). Such a session rehydrates as a history-less detached record and, on attach, starts a **fresh** Claude conversation (resume=None) rather than erroring.
- **A session that was mid-turn at restart is not actually running.** Rows with `status="working"` reflect the pre-crash state; the process is gone. Rehydrate must normalize `working` → `idle` so the UI never shows a phantom in-flight turn.
- **Test commands:** `.venv/bin/python -m pytest server/tests/ -q` (server). New tests go in `server/tests/`. All must stay green.
- **Commits:** conventional-commit messages; commit at the end of every task.

## Source-of-truth files (read before editing)

- `server/sessions/orchestrator.py` — `SessionOrchestrator`; owns `_records`, `_drivers`, `create_session`, `close_session`, `driver()`, `transcript_path()`, `_do_rotate()`.
- `server/sessions/driver.py` — `SessionDriver`; captures `self.claude_session_id` in `_drain()` (L105–106); `start()`/`send_prompt()`; ctor takes `resume=`.
- `server/sessions/types.py` — `SessionRecord` dataclass (the in-memory view).
- `server/db/models.py` — `Session` ORM model (all columns already exist).
- `server/db/__init__.py` — `init_db(db_url)`, `db_session()` async ctx manager.
- `server/app.py` — `_lifespan`; calls `init_db(db_url)` then `orchestrator.start()`.
- `server/bridge/ws.py` — `_handle_attach` (calls `orchestrator.driver()`, `transcript_path()`, `replay_events()`).
- `server/api/sessions.py` — `_record_to_dict` (hardcodes `status="working"`, `createdAt=None`).
- `src/ccswitch/configdir.py` — `compose(name) -> Path` (per-account config dir), `trust_project(name, cwd)`.
- `server/sessions/selection.py` — `select_account`, `AllAccountsBusy`.
- `server/tests/fakes/fake_sdk.py`, `fake_factory.py` — fake SDK; factory signature is `(cwd, config_dir, can_use_tool, mode, resume)`.

---

## Task 1 — Persist `claude_session_id` to the DB the moment the driver captures it

**Why first:** This is the foundational blocker. Today `claude_session_id` is captured only in memory (`driver._drain()` sets `self.claude_session_id = self._tr.session_id`) and written to the DB **only on rotation** (`_do_rotate` L406–412). After a normal restart the row's `claude_session_id` is still `NULL`, so no transcript can be located and no resume is possible. Persist it the first time it becomes non-null, on the normal (non-rotation) path.

**Files:**
- Modify `server/sessions/driver.py` — add an optional async callback fired once when `claude_session_id` is first captured.
- Modify `server/sessions/orchestrator.py` — pass a callback into every `SessionDriver` it constructs that UPDATEs the `DbSession` row.
- Create `server/tests/test_session_id_persistence.py`.

**Interfaces:**

*Consumes:* `Translator.session_id` (already set during `_drain`); `db_session()` from `server.db`; `sqlalchemy.update`.

*Produces:*
- `SessionDriver.__init__(..., on_session_id=None)` — `on_session_id: Callable[[str, str], Awaitable[None]] | None`, called as `await on_session_id(self.session_id, claude_session_id)` exactly once, the first time the Claude session id is captured.
- `SessionOrchestrator._persist_claude_session_id(session_id: str, claude_session_id: str) -> None` — UPDATEs `DbSession.claude_session_id` for that row.

### TDD steps

- [ ] **Step 1: Write the failing driver-callback test.**

```python
# server/tests/test_session_id_persistence.py
import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnEnd
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from claude_agent_sdk import TextBlock


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


def test_driver_fires_on_session_id_once_when_captured():
    msgs = [f.sys_init("sess-77"), f.stream_start(uuid="t1"),
            f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"),
            f.result(session_id="sess-77", uuid="t1")]
    calls = []

    async def on_sid(session_id, claude_session_id):
        calls.append((session_id, claude_session_id))

    async def run():
        d = SessionDriver("local-1", "primary", "/tmp/r", "/tmp/cfg",
                          mode="interactive", sdk_factory=_factory(msgs),
                          registry=PermissionRegistry(), identity="me",
                          on_session_id=on_sid)
        await d.start()
        await d.send_prompt("hi")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        await d.aclose()

    asyncio.run(run())
    assert calls == [("local-1", "sess-77")]   # exactly once, with both ids
```

Run: `.venv/bin/python -m pytest server/tests/test_session_id_persistence.py -q` → fails (`SessionDriver.__init__` has no `on_session_id`).

- [ ] **Step 2: Minimal driver implementation.**

In `server/sessions/driver.py`, add the parameter (keyword-only, default `None`) and store it:

```python
    def __init__(self, session_id, account, cwd, config_dir, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None,
                 on_session_id=None):
        ...
        self.resume = resume
        self.claude_session_id: str | None = None
        self._on_session_id = on_session_id
```

Then fire it in `_drain()` where the id is first captured (replace L105–106):

```python
                if self.claude_session_id is None and self._tr.session_id:
                    self.claude_session_id = self._tr.session_id
                    if self._on_session_id is not None:
                        try:
                            await self._on_session_id(self.session_id, self.claude_session_id)
                        except Exception:
                            log.exception("driver.on_session_id session=%s", self.session_id)
```

Run the test → passes.

- [ ] **Step 3: Write the failing orchestrator-persistence test.** Use a **file-backed** DB so the row is readable independently.

```python
def test_orchestrator_persists_claude_session_id_to_db(tmp_path):
    import asyncio
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    from server.tests.fakes import fake_sdk as f
    from claude_agent_sdk import TextBlock
    from unittest.mock import patch

    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    msgs = [f.sys_init("claude-abc"), f.stream_start(uuid="t1"),
            f.stream_text("ok", uuid="t1"),
            f.assistant(TextBlock(text="ok"), uuid="t1"),
            f.result(session_id="claude-abc", uuid="t1")]

    async def run():
        await init_db(db_url=db_url)
        with patch("server.sessions.orchestrator.compose", return_value=tmp_path/"cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"), \
             patch("server.sessions.orchestrator.select_account", return_value="alpha"):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(msgs),
                                       work_dir=str(tmp_path))
            rec = await orch.create_session("s", initial_prompt="hi")
            # let the turn drain so the id is captured + persisted
            for _ in range(50):
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))).scalar_one()
                    if row.claude_session_id == "claude-abc":
                        break
                await asyncio.sleep(0.02)
            await orch.close_session(rec.id)
            return row.claude_session_id

    assert asyncio.run(run()) == "claude-abc"

    asyncio.run(run())
```

Run → fails (orchestrator never wires `on_session_id`; row stays NULL).

- [ ] **Step 4: Minimal orchestrator implementation.** Add the persist helper and pass it into the `SessionDriver` constructed in `create_session` (and, for consistency, in `_do_rotate`):

```python
    async def _persist_claude_session_id(self, session_id: str, claude_session_id: str) -> None:
        # keep the in-memory record in sync too
        rec = self._records.get(session_id)
        if rec is not None:
            rec.claude_session_id = claude_session_id
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(claude_session_id=claude_session_id)
            )
```

In `create_session`, add `on_session_id=self._persist_claude_session_id` to the `SessionDriver(...)` call (L174). Do the same in `_do_rotate` (L394) so the new driver also persists if its id changes. Run the test → passes.

- [ ] **Step 5: Commit.**

```bash
git add server/sessions/driver.py server/sessions/orchestrator.py server/tests/test_session_id_persistence.py
git commit -m "feat(sessions): persist claude_session_id to DB on first capture"
```

**Edge cases covered here:** the callback fires at most once (guarded by `if self.claude_session_id is None`); a driver whose conversation never produces a session id (e.g. immediate failure) simply never persists — the row keeps `claude_session_id=NULL`, handled in later tasks.

---

## Task 2 — `orchestrator.rehydrate()` rebuilds detached `SessionRecord`s from non-ended rows

**Files:**
- Modify `server/sessions/types.py` — add fields needed to make a record self-describing without a driver.
- Modify `server/sessions/orchestrator.py` — add `rehydrate()`.
- Create `server/tests/test_orchestrator_rehydrate.py`.

**Interfaces:**

*Consumes:* `select(DbSession).where(DbSession.status != "ended")`; `db_session()`.

*Produces:*
- `SessionRecord` gains: `claude_session_id` already exists; add `detached: bool = False`, `status: str = "working"`, `permission_mode: str = "interactive"`, `created_at: datetime | None = None`. (`cols`/`rows` keep their current required positions; give the new fields defaults so existing call sites in `create_session` and `_do_rotate` keep working.)
- `SessionOrchestrator.rehydrate() -> int` — selects every `Session` row with `status != "ended"`, and for each id **not already in `_records`**, builds a detached `SessionRecord` (`detached=True`, no driver added to `_drivers`), normalizing `status="working"` → `"idle"`. Returns the count of records added.

**Type consistency note:** `SessionRecord.claude_session_id` is typed `str`; the DB column is `str | None`. Rehydrate must coerce NULL → `""` so the in-memory contract stays `str` (matching `create_session`, which already sets `claude_session_id=""`).

### TDD steps

- [ ] **Step 1: Add the new `SessionRecord` fields (compile-only change first).** Edit `server/sessions/types.py`:

```python
from datetime import datetime

@dataclass
class SessionRecord:
    id: str
    name: str
    account: str
    claude_session_id: str
    cols: int
    rows: int
    cwd: str
    detached: bool = False
    status: str = "idle"
    permission_mode: str = "interactive"
    created_at: datetime | None = None
```

- [ ] **Step 2: Write the failing rehydrate test.**

```python
# server/tests/test_orchestrator_rehydrate.py
import asyncio
from datetime import datetime
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def test_rehydrate_loads_non_ended_rows_as_detached(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="a", name="Alpha", account_name="acct1",
                 claude_session_id="claude-a", status="idle", cwd="/repo/a"),
            dict(id="b", name="Beta", account_name="acct2",
                 claude_session_id=None, status="working", cwd="/repo/b"),
            dict(id="c", name="Gone", account_name="acct1",
                 claude_session_id="claude-c", status="ended", cwd="/repo/c"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        n = await orch.rehydrate()
        return n, {r.id: r for r in orch.list_sessions()}, orch._drivers

    n, recs, drivers = asyncio.run(run())
    assert n == 2                       # a + b, not the ended c
    assert set(recs) == {"a", "b"}
    assert recs["a"].detached is True
    assert recs["a"].claude_session_id == "claude-a"
    assert recs["b"].claude_session_id == ""   # NULL coerced to ""
    assert recs["b"].status == "idle"          # working normalized to idle
    assert drivers == {}                        # lazy: no drivers spawned
```

Run → fails (`rehydrate` does not exist).

- [ ] **Step 3: Implement `rehydrate()`** in `server/sessions/orchestrator.py`:

```python
    async def rehydrate(self) -> int:
        """Rebuild detached SessionRecords from non-ended DB rows (lazy: no drivers).

        Idempotent: skips ids already present in _records. Normalizes a pre-crash
        'working' status to 'idle' (the process is gone). Returns rows added.
        """
        from sqlalchemy import select
        async with db_session() as db:
            rows = (await db.execute(
                select(DbSession).where(DbSession.status != "ended"))).scalars().all()
        added = 0
        for row in rows:
            if row.id in self._records:
                continue
            status = "idle" if row.status == "working" else row.status
            self._records[row.id] = SessionRecord(
                id=row.id,
                name=row.name,
                account=row.account_name,
                claude_session_id=row.claude_session_id or "",
                cols=80,
                rows=24,
                cwd=row.cwd,
                detached=True,
                status=status,
                permission_mode=row.permission_mode,
                created_at=row.created_at,
            )
            added += 1
        log.info("rehydrate: %d session(s) restored", added)
        return added
```

Run → passes.

- [ ] **Step 4: Add an idempotency test** in the same file:

```python
def test_rehydrate_is_idempotent(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([dict(id="a", name="A", account_name="x",
                          claude_session_id="cs", status="idle", cwd="/r")])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        first = await orch.rehydrate()
        second = await orch.rehydrate()    # no new rows added
        return first, second, len(orch.list_sessions())

    first, second, count = asyncio.run(run())
    assert (first, second, count) == (1, 0, 1)
```

Run → passes.

- [ ] **Step 5: Commit.**

```bash
git add server/sessions/types.py server/sessions/orchestrator.py server/tests/test_orchestrator_rehydrate.py
git commit -m "feat(sessions): rehydrate detached SessionRecords from non-ended DB rows"
```

---

## Task 3 — Wire `rehydrate()` into app startup (`_lifespan`)

**Files:**
- Modify `server/app.py` — call `await app.state.orchestrator.rehydrate()` after `init_db(...)` and before `orchestrator.start()`.
- Create `server/tests/test_app_rehydrate_on_startup.py`.

**Interfaces:**

*Consumes:* `orchestrator.rehydrate()` (Task 2); `init_db(db_url)`.

*Produces:* no new public API — startup behavior only. After app startup against a file-backed DB containing non-ended rows, `orchestrator.list_sessions()` is non-empty and `GET /api/sessions` returns those sessions.

**Ordering note:** `rehydrate()` must run **after** `init_db(db_url)` (the session factory must exist) and **before** `orchestrator.start()` (so the rotation monitor and any list call see the restored records). The call belongs between L57 (`await init_db(...)`) and L76 (`await app.state.orchestrator.start()`).

### TDD steps

- [ ] **Step 1: Write the failing startup test.** Pre-seed a file-backed DB, then build the app pointed at the same URL and assert the session is listed after lifespan startup.

```python
# server/tests/test_app_rehydrate_on_startup.py
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "rehydrate@example.com"}


def test_startup_rehydrates_sessions_into_list(tmp_path):
    db_path = tmp_path / "ccswitch.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    async def seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="restored", name="Restored", account_name="acct",
                             claude_session_id="claude-x", status="working",
                             cwd=str(tmp_path)))
    asyncio.run(seed())

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/sessions", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        ids = [s["id"] for s in resp.json()]
        assert "restored" in ids
```

Run → fails (lifespan never calls rehydrate; list is empty).

- [ ] **Step 2: Implement.** In `server/app.py` `_lifespan`, after the `await init_db(db_url=db_url)` line (L57):

```python
    await init_db(db_url=db_url)

    # Rehydrate persisted sessions (lazy — drivers are created on attach, not here).
    await app.state.orchestrator.rehydrate()
```

Run → passes.

- [ ] **Step 3: Commit.**

```bash
git add server/app.py server/tests/test_app_rehydrate_on_startup.py
git commit -m "feat(app): rehydrate sessions on startup before orchestrator.start()"
```

**Note for reviewers:** with the default `:memory:` DB the seeded row and the app's engine are different databases, so this behavior is inert in dev/test-without-env — exactly the Global Constraint above. The test pins `CCSWITCH_DB_URL` to the same file used for seeding.

---

## Task 4 — Lazy resume on attach: build+start a resumed driver for a detached session

**Why:** `_handle_attach` (`server/bridge/ws.py` L122–139) calls `orchestrator.driver(session_id)`; for a rehydrated session that returns `None`, so attach currently 404s (`SESSION_NOT_FOUND`). Make the orchestrator able to **materialize** a live driver for a detached record on demand, reusing the stored `claude_session_id` as `resume`.

**Files:**
- Modify `server/sessions/orchestrator.py` — add `ensure_driver(session_id)` and have the WS attach path use it.
- Modify `server/bridge/ws.py` — `_handle_attach` calls `await orchestrator.ensure_driver(session_id)` instead of the sync `orchestrator.driver(session_id)`.
- Create `server/tests/test_orchestrator_lazy_resume.py`.

**Interfaces:**

*Consumes:* the detached `SessionRecord` (Task 2); `compose(account) -> Path`, `configdir.trust_project`; `self._sdk_factory`; `self._registry`; `self._persist_claude_session_id` (Task 1).

*Produces:*
- `SessionOrchestrator.ensure_driver(session_id: str) -> SessionDriver | None` — if a live driver exists, return it. If the record is detached, construct a `SessionDriver` with `resume=record.claude_session_id or None`, using the record's `cwd`, `account` (→ `compose(account)` for `config_dir`), `permission_mode`, `start()` it, register it in `_drivers`, flip `record.detached = False`, and return it. If the session id is unknown (no record), return `None`.

**Resume value:** pass `resume=(record.claude_session_id or None)`. A non-empty stored id → the SDK resumes the existing Claude conversation. An empty id (NULL at restart) → `resume=None` → a fresh conversation under the same ccswitch session id (history-less, but usable). This is the Global Constraint behavior for never-responded sessions.

### TDD steps

- [ ] **Step 1: Write the failing lazy-resume test.** Rehydrate a detached row, call `ensure_driver`, assert a driver is created and the fake factory received the stored id as `resume`.

```python
# server/tests/test_orchestrator_lazy_resume.py
import asyncio
from unittest.mock import patch
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator


def _capturing_factory(captured):
    from server.tests.fakes.fake_sdk import FakeSDKClient
    def make(cwd, config_dir, can_use_tool, mode, resume):
        captured.append({"cwd": cwd, "config_dir": str(config_dir),
                         "mode": mode, "resume": resume})
        return FakeSDKClient([], on_can_use_tool=can_use_tool)
    return make


def test_ensure_driver_resumes_detached_session(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    captured = []

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="S", account_name="alpha",
                             claude_session_id="claude-resume-me",
                             status="idle", cwd="/repo/x",
                             permission_mode="plan"))
        with patch("server.sessions.orchestrator.compose", return_value=tmp_path/"cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            orch = SessionOrchestrator(sdk_factory=_capturing_factory(captured))
            await orch.rehydrate()
            assert orch.driver("s1") is None         # detached: no live driver yet
            d = await orch.ensure_driver("s1")
            assert d is not None
            assert orch.driver("s1") is d            # now registered
            assert orch.get_session("s1").detached is False
            await orch.close_session("s1")
        return captured

    captured = asyncio.run(run())
    assert captured and captured[0]["resume"] == "claude-resume-me"
    assert captured[0]["cwd"] == "/repo/x"
    assert captured[0]["mode"] == "plan"
```

Run → fails (`ensure_driver` does not exist).

- [ ] **Step 2: Implement `ensure_driver()`** in `server/sessions/orchestrator.py`:

```python
    async def ensure_driver(self, session_id: str) -> SessionDriver | None:
        """Return the live driver for session_id, creating+starting a resumed
        one for a detached (rehydrated) session on demand. None if unknown."""
        existing = self._drivers.get(session_id)
        if existing is not None:
            return existing
        record = self._records.get(session_id)
        if record is None:
            return None
        cfg_dir = compose(record.account)
        configdir.trust_project(record.account, record.cwd)
        driver = SessionDriver(
            session_id, record.account, record.cwd, str(cfg_dir),
            mode=record.permission_mode, sdk_factory=self._sdk_factory,
            registry=self._registry, identity="system",
            resume=(record.claude_session_id or None),
            on_session_id=self._persist_claude_session_id,
        )
        await driver.start()
        self._drivers[session_id] = driver
        record.detached = False
        log.info("session.resume id=%s resume=%s", session_id,
                 record.claude_session_id or "<fresh>")
        return driver
```

Run the orchestrator test → passes.

- [ ] **Step 3: Wire the WS attach path.** In `server/bridge/ws.py` `_handle_attach`, change L126 from the sync accessor to the async resumer:

```python
            driver = await orchestrator.ensure_driver(session_id)
            if driver is None:
                await _send({"type": "error", "code": "SESSION_NOT_FOUND", ...})
                return
```

(The rest of `_handle_attach` — `transcript_path`, replay, live stream — is unchanged.)

- [ ] **Step 4: Add a WS-level regression test** in `server/tests/test_ws.py` (mirror its existing `_seed_account` / app-with-fake-factory fixture): rehydrate a detached row, open the WS, send `{"type":"attach","sessionId": "..."}`, and assert a `replayComplete` frame arrives (i.e. attach no longer 404s on a rehydrated session). Reuse the file's existing identity headers and TestClient WS pattern. Run the WS tests → green.

- [ ] **Step 5: Commit.**

```bash
git add server/sessions/orchestrator.py server/bridge/ws.py server/tests/test_orchestrator_lazy_resume.py server/tests/test_ws.py
git commit -m "feat(sessions): lazily resume a detached session's driver on WS attach"
```

**Edge case — identity:** `ensure_driver` binds `identity="system"` because the resuming user's identity is the WS handshake identity, not stored on the row. Permission prompts on a resumed session therefore behave as the create-time default did pre-bugfix; if per-user permission matching on resume is required, thread `identity` from `_handle_attach` into `ensure_driver(session_id, identity=...)` as a follow-up. Note this in the commit body so it is not silently lost.

---

## Task 5 — `transcript_path()` works for a detached session from the DB row

**Why:** `transcript_path()` (`orchestrator.py` L334–341) reads `self._drivers[session_id]` for `cwd`, `config_dir`, and `claude_session_id`. A rehydrated session has no driver until attach, and `_handle_attach` calls `transcript_path()` right after `ensure_driver()`. With Task 4, by the time `transcript_path` is called the driver exists — **but** the transcript must be locatable for replay even though the driver's `claude_session_id` is `None` until the first new turn drains. The stored id lives on the **record**, not the freshly-started driver. Make `transcript_path` fall back to the record + composed config dir.

**Files:**
- Modify `server/sessions/orchestrator.py` — `transcript_path()` falls back to the `SessionRecord` + `compose(account)` when the driver has no captured id.
- Create `server/tests/test_transcript_path_detached.py`.

**Interfaces:**

*Consumes:* `SessionRecord` (`cwd`, `account`, `claude_session_id`); `compose(account) -> Path`.

*Produces:* `transcript_path(session_id) -> str | None` — unchanged signature; now resolves the JSONL path from the record when the driver is absent or its `claude_session_id` is still `None`, returning the path only if the file exists.

**Path formula (must match the existing one exactly):** `<config_dir>/projects/<cwd-key>/<claude_session_id>.jsonl` where `cwd_key = cwd.lstrip("/").replace("/", "-")`.

### TDD steps

- [ ] **Step 1: Write the failing test.** Create a fake transcript file under a composed config dir, rehydrate a row whose `claude_session_id`/`cwd` point at it, and assert `transcript_path` returns it even with **no driver**.

```python
# server/tests/test_transcript_path_detached.py
import asyncio
from pathlib import Path
from unittest.mock import patch
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def test_transcript_path_for_detached_session(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    cfg = tmp_path / "cfg"
    cwd = "/repo/proj"
    cwd_key = cwd.lstrip("/").replace("/", "-")          # "repo-proj"
    sid = "claude-xyz"
    transcript = cfg / "projects" / cwd_key / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="d1", name="D", account_name="alpha",
                             claude_session_id=sid, status="idle", cwd=cwd))
        with patch("server.sessions.orchestrator.compose", return_value=cfg):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            await orch.rehydrate()
            assert orch.driver("d1") is None              # no driver
            return orch.transcript_path("d1")

    assert asyncio.run(run()) == str(transcript)
```

Run → fails (current `transcript_path` returns `None` when there's no driver).

- [ ] **Step 2: Implement the fallback** in `transcript_path()`:

```python
    def transcript_path(self, session_id) -> str | None:
        """<config_dir>/projects/<cwd-key>/<claude_session_id>.jsonl, or None."""
        d = self._drivers.get(session_id)
        if d is not None and d.claude_session_id:
            cwd, config_dir, claude_sid = d.cwd, d.config_dir, d.claude_session_id
        else:
            rec = self._records.get(session_id)
            if rec is None or not rec.claude_session_id:
                return None
            cwd, config_dir, claude_sid = rec.cwd, str(compose(rec.account)), rec.claude_session_id
        cwd_key = cwd.lstrip("/").replace("/", "-")
        p = Path(config_dir) / "projects" / cwd_key / f"{claude_sid}.jsonl"
        return str(p) if p.exists() else None
```

Run → passes. Also run `server/tests/test_ws.py` and Task 4's tests to confirm the live path still works.

- [ ] **Step 3: Add a missing-transcript test** (file absent → `None`): same setup but do not create the `.jsonl`; assert `transcript_path("d1") is None`. Run → passes (covers the "missing transcript file" edge case; `_handle_attach` already treats `None` as "no replay, just `replayComplete`").

- [ ] **Step 4: Commit.**

```bash
git add server/sessions/orchestrator.py server/tests/test_transcript_path_detached.py
git commit -m "feat(sessions): resolve transcript_path for detached sessions from the DB row"
```

---

## Task 6 — Surface rehydrated session state to the UI (`_record_to_dict`)

**Why:** `server/api/sessions.py` `_record_to_dict` (L59–68) hardcodes `status="working"` and `createdAt=None` with the comment "in-memory sessions are always working." After rehydration that is false: a restored session is `idle` (or whatever its row said) and has a real `created_at`. The list must show a rehydrated session truthfully so the UI presents it as resumable rather than perpetually "working."

**Files:**
- Modify `server/api/sessions.py` — `_record_to_dict` reads `record.status` and `record.created_at` (added in Task 2) instead of hardcoding.
- Create / extend `server/tests/test_api_rehydrate_list.py`.

**Interfaces:**

*Consumes:* `SessionRecord.status`, `SessionRecord.created_at` (Task 2), `SessionRecord.detached`.

*Produces:* `_record_to_dict(record) -> dict` with `status = record.status`, `createdAt = record.created_at.isoformat() if record.created_at else None`. Optionally include `"detached": record.detached` so the frontend can badge resumable sessions.

**Back-compat note:** live (non-rehydrated) records created via `create_session` get `status` from the dataclass default. To keep their list status accurate, set `record.status` in `create_session` to `"working" if initial_prompt else "idle"` (mirroring the DB row it already writes at L192) when constructing the `SessionRecord` (L199–207). This keeps the freshly-created and rehydrated paths consistent through one field.

### TDD steps

- [ ] **Step 1: Write the failing test.** Rehydrate an `idle` row with a known `created_at`; assert the API list reports `status="idle"` and a non-null `createdAt`.

```python
# server/tests/test_api_rehydrate_list.py
import asyncio, os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "list@example.com"}


def test_rehydrated_session_reports_real_status_and_created_at(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    created = datetime(2026, 6, 20, 12, 0, 0)

    async def seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="r1", name="R", account_name="acct",
                             claude_session_id="cs", status="idle",
                             cwd=str(tmp_path), created_at=created))
    asyncio.run(seed())

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        body = client.get("/api/sessions", headers=_IDENT).json()
        sess = next(s for s in body if s["id"] == "r1")
        assert sess["status"] == "idle"
        assert sess["createdAt"] == created.isoformat()
```

Run → fails (hardcoded `status="working"`, `createdAt=None`).

- [ ] **Step 2: Implement.** Edit `_record_to_dict`:

```python
def _record_to_dict(record) -> dict[str, Any]:
    """Convert an in-memory SessionRecord to the wire-format dict."""
    return {
        "id": record.id,
        "name": record.name,
        "accountName": record.account,
        "status": record.status,
        "detached": record.detached,
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "updatedAt": None,
    }
```

And in `server/sessions/orchestrator.py` `create_session`, set `status=` on the constructed `SessionRecord` (L199):

```python
        record = SessionRecord(
            id=session_id, name=name, account=account,
            claude_session_id="", cols=cols, rows=rows, cwd=cwd,
            status="working" if initial_prompt else "idle",
            permission_mode=permission_mode,
        )
```

Run → passes. Re-run the full server suite to confirm nothing relying on the old hardcoded `"working"` broke (`.venv/bin/python -m pytest server/tests/ -q`).

- [ ] **Step 3: Commit.**

```bash
git add server/api/sessions.py server/sessions/orchestrator.py server/tests/test_api_rehydrate_list.py
git commit -m "feat(api): report real status/createdAt for sessions incl. rehydrated"
```

---

## Self-review (writing-plans discipline)

**Spec coverage:** all four approved parts are decomposed — (1) persist `claude_session_id` = Task 1; (2) `rehydrate()` = Task 2; (3) wire into `_lifespan` = Task 3; (4) lazy resume on attach = Task 4. Plus the two supporting items the spec called out: `transcript_path()` for detached sessions = Task 5, and the `_record_to_dict` status/createdAt surface = Task 6.

**Edge cases placed in the owning task:** NULL `claude_session_id` (Task 1 guard + Task 2 NULL→"" coercion + Task 4 `resume=None` fresh-start); mid-turn `working` at restart → `idle` (Task 2 normalization); missing transcript file (Task 5 Step 3 → `None`, already handled by `_handle_attach`); idempotent re-run (Task 2 Step 4).

**Global Constraint recorded:** file-backed vs `:memory:` DB — rehydration only helps with a file-backed `CCSWITCH_DB_URL`, and every restart-simulating test pins a `tmp_path` file URL (Tasks 1, 2, 3, 4, 5, 6 all use `sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}`).

**No placeholders:** every task has real file paths, real signatures, runnable test snippets, and exact code following the existing idioms (fake SDK factory signature `(cwd, config_dir, can_use_tool, mode, resume)`; `db_session()` ctx manager; `sqlalchemy.update`/`select`; `compose`/`trust_project` patched in tests).

**Type consistency:** `SessionRecord.claude_session_id` stays `str` (NULL coerced to `""`); the resume value passed to the SDK is `record.claude_session_id or None` so the typed-`str` record and the `str | None` SDK `resume` parameter never disagree; new `SessionRecord` fields have defaults so existing `create_session`/`_do_rotate` constructions keep compiling.
