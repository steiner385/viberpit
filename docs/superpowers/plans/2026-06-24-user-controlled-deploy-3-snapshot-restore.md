# User-Controlled Deploy — Plan 3: Session Snapshot/Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the controller's SAVE (step 1) and RESUME (step 5) phases real: snapshot which sessions were *live* before a daemon-affecting restart, then **eagerly re-launch** them afterward (instead of today's lazy wait-for-attach), surfacing per-session restore results — keep the (healthy) deploy and flag any sessions that failed to come back.

**Architecture:** Two new daemon RPCs on `SessionOrchestrator` — `snapshot_deploy()` writes the live-session list to a durable artifact, `restore_deploy()` reads it back in the fresh daemon and `ensure_driver()`s each session, returning per-session ok/fail. The Plan 1 `DeployController` calls them (over its existing `DaemonClient`) around the restart: SAVE before update, RESUME after the health gate (skipped for edge-only deploys, where the daemon never restarted). The web `DeployPanel` (Plan 2) gains a restore-results section.

**Tech Stack:** Python asyncio, SQLAlchemy (no schema change — artifact is a JSON file), React. No new dependencies.

## Why this scope is small (read before implementing)

ccswitch sessions are **SDK/transcript-based, not raw PTYs** (`server/bridge/ws.py`: "no PTY bytes"). Consequences verified in the codebase:
- **Scrollback already survives a restart for free** — history is the on-disk transcript JSONL, replayed by `server/sessions/replay.py:replay_window` on attach. No scrollback bytes to snapshot.
- **Unsent input is browser-side** — composer drafts persist via `localStorage` (`web/src/hooks/useAutoReload.ts`), surviving reload. No server capture needed.
- **The re-launch recipe already persists** — the `Session` DB row (account, cwd, branch, `claude_session_id`) + `rehydrate()`. But rehydrate is **lazy** (rebuilds *detached*, PTY spun on attach) and **lossy** (normalizes a pre-restart `working` status to `idle`).

So the only genuinely-new work is: **capture liveness** (which sessions had a live driver, lost today) and **eagerly re-launch** them on restore. That is this plan.

## Global Constraints

- Depends on Plans 1 + 2 merged.
- Liveness source = `SessionOrchestrator._drivers` (a live driver) — same notion as `has_live_session`/`live_session_load`. Active set = `_ACTIVE_STATUSES = {in_progress, working, waiting_input, condensing}`.
- Re-launch primitive = `await orch.ensure_driver(session_id, identity="system")` (resumes a detached session from DB; returns `None` if unknown).
- No NOT NULL columns (dev SQLite never ALTERs); this plan adds **no columns** — state lives in a JSON artifact.
- Artifact path: `CCSWITCH_DEPLOY_SNAPSHOT` or default `/var/lib/ccswitch/deploy-snapshot/live.json`; create the dir with `exist_ok=True`.
- Backend tests: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest`.

---

## File Structure

- Create `server/sessions/deploy_snapshot.py` — pure read/write of the artifact (`write_snapshot`, `read_snapshot`, `clear_snapshot`, `snapshot_path`).
- Modify `server/sessions/orchestrator.py` — add `snapshot_deploy()` and `restore_deploy()` methods.
- Modify `server/ipc/orchestrator_rpc.py` — register both RPCs.
- Modify `server/sessions/remote.py` — add pass-through methods on `RemoteOrchestrator` (edge-side facade) if the controller talks via it; the controller uses `DaemonClient` directly, so this is only needed if the edge surfaces them (it does not — skip unless a test requires it).
- Modify `server/deploy/types.py` — `DeployStatus` gains `restore: dict` (`{"restored": [...], "failed": [...]}`).
- Modify `server/deploy/controller.py` — drive SAVE + RESUME phases via the daemon client.
- Modify `server/deploy/app.py` — include `restore` in `_deploy_dict()`.
- Modify `web/src/api/types.ts` — `DeployProgress.restore`.
- Modify `web/src/components/DeployPanel.tsx` — restore-results section.
- Tests: `server/tests/test_deploy_snapshot.py`, `test_orchestrator_snapshot_restore.py`, extend `test_deploy_controller.py`, `web/src/components/DeployPanel.test.tsx`.

---

## Task 1: Snapshot artifact read/write (`deploy_snapshot.py`)

**Files:**
- Create: `server/sessions/deploy_snapshot.py`
- Test: `server/tests/test_deploy_snapshot.py`

**Interfaces:**
- Produces:
  - `snapshot_path() -> str` — `CCSWITCH_DEPLOY_SNAPSHOT` or the default.
  - `write_snapshot(live: list[dict], *, path: str | None = None) -> str` — writes `{"live": [...]}` JSON (each `{"id","status"}`); creates the parent dir; returns the path.
  - `read_snapshot(*, path: str | None = None) -> list[dict]` — returns `live` or `[]` if absent/corrupt.
  - `clear_snapshot(*, path: str | None = None) -> None` — unlink if present (no error if missing).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_snapshot.py
from server.sessions import deploy_snapshot as ds


def test_write_then_read_roundtrip(tmp_path):
    p = str(tmp_path / "sub" / "live.json")   # parent dir does not exist yet
    ds.write_snapshot([{"id": "s1", "status": "working"}], path=p)
    assert ds.read_snapshot(path=p) == [{"id": "s1", "status": "working"}]


def test_read_missing_is_empty(tmp_path):
    assert ds.read_snapshot(path=str(tmp_path / "nope.json")) == []


def test_read_corrupt_is_empty(tmp_path):
    p = tmp_path / "live.json"; p.write_text("{not json")
    assert ds.read_snapshot(path=str(p)) == []


def test_clear_removes_file(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "idle"}], path=p)
    ds.clear_snapshot(path=p)
    assert ds.read_snapshot(path=p) == []
    ds.clear_snapshot(path=p)   # idempotent — no error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.sessions.deploy_snapshot`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/deploy_snapshot.py
from __future__ import annotations
import json
import os

DEFAULT_PATH = "/var/lib/ccswitch/deploy-snapshot/live.json"


def snapshot_path() -> str:
    return os.environ.get("CCSWITCH_DEPLOY_SNAPSHOT", DEFAULT_PATH)


def write_snapshot(live: list[dict], *, path: str | None = None) -> str:
    p = path or snapshot_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        json.dump({"live": live}, fh)
    return p


def read_snapshot(*, path: str | None = None) -> list[dict]:
    p = path or snapshot_path()
    try:
        with open(p) as fh:
            return json.load(fh).get("live", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def clear_snapshot(*, path: str | None = None) -> None:
    p = path or snapshot_path()
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_snapshot.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/deploy_snapshot.py server/tests/test_deploy_snapshot.py
git commit -m "feat(sessions): durable deploy-snapshot artifact read/write"
```

---

## Task 2: Orchestrator `snapshot_deploy()` + `restore_deploy()`

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_orchestrator_snapshot_restore.py`

**Interfaces:**
- Consumes: `self._drivers`, `self._records`, `await self.ensure_driver(...)`, `deploy_snapshot`.
- Produces (methods on `SessionOrchestrator`):
  - `def snapshot_deploy(self, *, path: str | None = None) -> dict` — collects `[{"id": sid, "status": rec.status}]` for every `sid` in `_drivers` (snapshot the keys first — the event loop mutates `_drivers`), writes via `deploy_snapshot.write_snapshot`, returns `{"live": [...]}`.
  - `async def restore_deploy(self, *, path: str | None = None) -> dict` — reads the artifact; for each entry `await ensure_driver(id, "system")`; collects `restored`/`failed` (failed = `ensure_driver` returned None or raised). Clears the artifact on completion. Returns `{"restored": [...ids], "failed": [...ids]}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_snapshot_restore.py
import pytest
from server.sessions import deploy_snapshot as ds


class FakeRec:
    def __init__(self, status): self.status = status


class MiniOrch:
    """Exercises the snapshot/restore logic with the real functions bound in."""
    from server.sessions.orchestrator import SessionOrchestrator
    snapshot_deploy = SessionOrchestrator.snapshot_deploy
    restore_deploy = SessionOrchestrator.restore_deploy

    def __init__(self, drivers, records, resumable):
        self._drivers = drivers
        self._records = records
        self._resumable = resumable        # set of ids ensure_driver succeeds for
        self.ensured: list[str] = []

    async def ensure_driver(self, session_id, identity="system"):
        self.ensured.append(session_id)
        return object() if session_id in self._resumable else None


def test_snapshot_captures_live_drivers(tmp_path):
    p = str(tmp_path / "live.json")
    o = MiniOrch(drivers={"s1": 1, "s2": 1},
                 records={"s1": FakeRec("working"), "s2": FakeRec("waiting_input")},
                 resumable=set())
    snap = o.snapshot_deploy(path=p)
    ids = {e["id"] for e in snap["live"]}
    assert ids == {"s1", "s2"}
    assert ds.read_snapshot(path=p) == snap["live"]


@pytest.mark.asyncio
async def test_restore_relaunches_and_splits_results(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "working"}, {"id": "s2", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"] and res["failed"] == ["s2"]
    assert set(o.ensured) == {"s1", "s2"}
    assert ds.read_snapshot(path=p) == []   # cleared
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_orchestrator_snapshot_restore.py -v`
Expected: FAIL — `AttributeError: ... 'snapshot_deploy'`.

- [ ] **Step 3: Write minimal implementation (add methods to `SessionOrchestrator`)**

```python
# near the top of orchestrator.py
from server.sessions import deploy_snapshot as _deploy_snapshot

# ... inside class SessionOrchestrator:

    def snapshot_deploy(self, *, path: str | None = None) -> dict:
        """SAVE step: record sessions with a live driver so a daemon-affecting restart
        can eagerly re-launch them (rehydrate alone is lazy + flattens 'working'→'idle')."""
        live = [{"id": sid, "status": getattr(self._records.get(sid), "status", "idle")}
                for sid in list(self._drivers.keys())]
        _deploy_snapshot.write_snapshot(live, path=path)
        return {"live": live}

    async def restore_deploy(self, *, path: str | None = None) -> dict:
        """RESUME step: eagerly re-launch each snapshotted session in the fresh daemon."""
        restored: list[str] = []
        failed: list[str] = []
        for entry in _deploy_snapshot.read_snapshot(path=path):
            sid = entry["id"]
            try:
                driver = await self.ensure_driver(sid, "system")
            except Exception:
                driver = None
            (restored if driver is not None else failed).append(sid)
        _deploy_snapshot.clear_snapshot(path=path)
        return {"restored": restored, "failed": failed}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_orchestrator_snapshot_restore.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_snapshot_restore.py
git commit -m "feat(sessions): snapshot_deploy (capture liveness) + restore_deploy (eager re-launch)"
```

---

## Task 3: Register snapshot/restore RPCs

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_orchestrator_snapshot_restore.py` (extend with an RPC-surface check)

**Interfaces:**
- Produces: daemon RPC methods `snapshot_deploy` (sync, dict) and `restore_deploy` (async, dict), callable via `DaemonClient.call("snapshot_deploy")` / `call("restore_deploy")`.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_snapshot_restore_registered_as_rpc():
    import inspect
    from server.ipc import orchestrator_rpc
    src = inspect.getsource(orchestrator_rpc.register_orchestrator_rpc)
    assert "snapshot_deploy" in src and "restore_deploy" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_orchestrator_snapshot_restore.py::test_snapshot_restore_registered_as_rpc -v`
Expected: FAIL — names absent from the registration function.

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/orchestrator_rpc.py`, add both names to the `_SIMPLE` list (both return JSON-safe dicts; the `_make_simple` wrapper already `await`s coroutines via `_maybe_await`):

```python
    _SIMPLE = [
        # ... existing entries ...
        "begin_drain", "drain_status",
        "live_session_load", "live_session_counts",
        "snapshot_deploy", "restore_deploy",
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_orchestrator_snapshot_restore.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/tests/test_orchestrator_snapshot_restore.py
git commit -m "feat(ipc): expose snapshot_deploy/restore_deploy over the daemon RPC surface"
```

---

## Task 4: Wire SAVE + RESUME into `DeployController`

**Files:**
- Modify: `server/deploy/types.py` (add `restore` field), `server/deploy/controller.py`
- Test: `server/tests/test_deploy_controller.py` (extend)

**Interfaces:**
- Consumes: the daemon client from `daemon_client_factory` (already in Plan 1's controller); `client.call("snapshot_deploy")`, `client.call("restore_deploy")`.
- Produces:
  - `DeployStatus.restore: dict = field(default_factory=dict)` — `{"restored": [...], "failed": [...]}`.
  - In `deploy_once`: **SAVE phase** (after preflight, before update) — if `daemon_client_factory`, `await client.call("snapshot_deploy")`; mark SAVE done (best-effort: a snapshot failure is a warning, not an abort). **RESUME phase** (after a successful daemon-affecting restart) — `self.status.restore = await client.call("restore_deploy")`; mark RESUME done; if `restore["failed"]`, append a warning (deploy still stands). Edge-only deploys skip both SAVE re-launch relevance and RESUME (daemon kept everything).

- [ ] **Step 1: Write the failing test (extend `test_deploy_controller.py`)**

```python
@pytest.mark.asyncio
async def test_daemon_deploy_snapshots_and_restores():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"

    rpc_calls = []
    class FakeDaemon:
        async def call(self, method, **params):
            rpc_calls.append(method)
            if method == "begin_drain": return {}
            if method == "drain_status": return {"state": "ready"}
            if method == "snapshot_deploy": return {"live": [{"id": "s1", "status": "working"}]}
            if method == "restore_deploy": return {"restored": ["s1"], "failed": ["s2"]}
            raise AssertionError(method)

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert "snapshot_deploy" in rpc_calls and "restore_deploy" in rpc_calls
    assert st.restore == {"restored": ["s1"], "failed": ["s2"]}
    assert any("s2" in w for w in st.warnings)        # failed restore flagged, deploy stands
    assert st.phases["save"] == "done" and st.phases["resume"] == "done"
    assert st.error is None


@pytest.mark.asyncio
async def test_edge_only_skips_snapshot_and_restore():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    rpc_calls = []
    class FakeDaemon:
        async def call(self, method, **params):
            rpc_calls.append(method); return {}
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert "restore_deploy" not in rpc_calls
    assert st.error is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_controller.py -v -k "snapshot or edge_only_skips"`
Expected: FAIL — controller does not call snapshot/restore; `DeployStatus` has no `restore`.

- [ ] **Step 3: Write minimal implementation**

Add to `DeployStatus` in `server/deploy/types.py`:

```python
    restore: dict = field(default_factory=dict)
```

In `server/deploy/controller.py` `deploy_once`, after the preflight `self._set(Phase.PREFLIGHT, PhaseState.DONE)` and before update:

```python
            # --- save (snapshot live sessions; best-effort) ---
            self._set(Phase.SAVE, PhaseState.RUNNING)
            if self._client_factory is not None:
                try:
                    await self._client_factory().call("snapshot_deploy")
                except Exception:
                    self.status.warnings.append("session snapshot failed (continuing)")
            self._set(Phase.SAVE, PhaseState.DONE)
```

After the restart phase's health gate passes (`self._set(Phase.RESTART, PhaseState.DONE)`), add:

```python
            # --- resume (eager re-launch; only when the daemon actually restarted) ---
            if daemon_changed and self._client_factory is not None:
                self._set(Phase.RESUME, PhaseState.RUNNING)
                try:
                    self.status.restore = await self._client_factory().call("restore_deploy")
                    failed = self.status.restore.get("failed", [])
                    if failed:
                        self.status.warnings.append(f"resume failed for {len(failed)} session(s): {', '.join(failed)}")
                except Exception:
                    self.status.warnings.append("session restore call failed")
                self._set(Phase.RESUME, PhaseState.DONE)
```

(For edge-only deploys `daemon_changed` is False → RESUME is left `pending`, which the UI shows as skipped; sessions were never dropped.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_controller.py -v`
Expected: PASS (all controller tests, incl. the two new ones).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/types.py server/deploy/controller.py server/tests/test_deploy_controller.py
git commit -m "feat(deploy): wire SAVE/RESUME phases — snapshot then eager re-launch (flag failed restores)"
```

---

## Task 5: Surface `restore` in controller status + edge passthrough

**Files:**
- Modify: `server/deploy/app.py` (`_deploy_dict`)
- Test: `server/tests/test_deploy_app.py` (extend)

**Interfaces:** `_deploy_dict()` includes `"restore": s.restore`.

- [ ] **Step 1: Write the failing test (append to `test_deploy_app.py`)**

```python
def test_status_includes_restore(client):
    c, ctrl = client
    ctrl.status.restore = {"restored": ["s1"], "failed": ["s2"]}
    body = c.get("/status").json()
    assert body["deploy"]["restore"] == {"restored": ["s1"], "failed": ["s2"]}
```

(Update the `FakeController` in `test_deploy_app.py` so its `DeployStatus(...)` constructions are compatible — `restore` defaults to `{}`, so no change needed beyond this assertion.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_app.py::test_status_includes_restore -v`
Expected: FAIL — `KeyError: 'restore'`.

- [ ] **Step 3: Write minimal implementation**

In `server/deploy/app.py` `_deploy_dict()`:

```python
        return {"running": s.running, "phases": s.phases, "error": s.error,
                "warnings": s.warnings, "targetSha": s.target_sha, "restore": s.restore}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_app.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/deploy/app.py server/tests/test_deploy_app.py
git commit -m "feat(deploy): expose per-session restore results in controller status"
```

---

## Task 6: Web — restore results in `DeployPanel`

**Files:**
- Modify: `web/src/api/types.ts` (`DeployProgress.restore`), `web/src/components/DeployPanel.tsx`
- Test: `web/src/components/DeployPanel.test.tsx` (extend)

**Interfaces:**
- `DeployProgress.restore?: { restored: string[]; failed: string[] }`.
- `DeployPanel` renders a restore section when `deploy.restore?.failed?.length`: "M session(s) failed to resume — open them to retry" + the ids. (Retry = opening the session; attaching triggers `ensure_driver` server-side, so no new endpoint is needed.)

- [ ] **Step 1: Write the failing test (append to `DeployPanel.test.tsx`)**

```tsx
it('flags sessions that failed to resume', () => {
  mockHooks({ behind: false, commits: [], ci: 'green' },
            { running: false, phases: { resume: 'done' }, warnings: [],
              restore: { restored: ['s1'], failed: ['s2', 's3'] } })
  render(<DeployPanel />)
  expect(screen.getByText(/2 session.*failed to resume/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/DeployPanel.test.tsx`
Expected: FAIL — restore text not rendered.

- [ ] **Step 3: Write minimal implementation**

Add to `DeployProgress` in `web/src/api/types.ts`:

```ts
  restore?: { restored: string[]; failed: string[] }
```

In `DeployPanel.tsx`, inside the progress block (after the phase list), add:

```tsx
          {deploy.restore?.failed?.length ? (
            <p className="mt-1 text-xs text-red-600">
              {deploy.restore.failed.length} session{deploy.restore.failed.length === 1 ? '' : 's'} failed to resume — open them to retry:{' '}
              <span className="font-mono">{deploy.restore.failed.join(', ')}</span>
            </p>
          ) : null}
```

- [ ] **Step 4: Run the web gate, then commit**

Run:
```bash
cd web
npx vitest run src/components/DeployPanel.test.tsx
npx tsc -b
npx eslint src/components/DeployPanel.tsx --max-warnings 0
```
Expected: PASS; tsc + eslint clean.

```bash
git add web/src/api/types.ts web/src/components/DeployPanel.tsx web/src/components/DeployPanel.test.tsx
git commit -m "feat(web): surface failed session restores in DeployPanel (open-to-retry)"
```

---

## Task 7: Full-suite verification

**Files:** none (verification task).

- [ ] **Step 1: Backend deploy + sessions suites**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src \
  <main-checkout>/.venv/bin/python -m pytest \
  server/tests/test_deploy_*.py server/tests/test_orchestrator_snapshot_restore.py \
  server/tests/test_api_deploy.py -q
```
Expected: all PASS.

- [ ] **Step 2: Backend lint**

Run: `ruff check server/deploy/ server/sessions/deploy_snapshot.py server/api/deploy.py && mypy server/deploy/ server/sessions/deploy_snapshot.py`
Expected: clean.

- [ ] **Step 3: Web gate**

Run: `cd web && npx vitest run src/components/DeployPanel src/api/hooks.deploy && npx tsc -b && npx eslint src --max-warnings 0`
Expected: all PASS; clean.

- [ ] **Step 4: Confirm no parallel implementations introduced**

Run: `grep -rn "scrollback\|unsent" server/deploy server/sessions/deploy_snapshot.py`
Expected: prints nothing — we deliberately did NOT build scrollback/unsent-input capture (transcript + localStorage already cover them).

- [ ] **Step 5: Commit (if any lint fixups)**

```bash
git add -A && git commit -m "test(deploy): full snapshot/restore suite + lint green" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage (Plan 3 scope):**
- Explicit snapshot before restart → Tasks 1–2, 4 (SAVE). ✓
- Eager re-launch on restore (vs lazy rehydrate) → Task 2 `restore_deploy` + Task 4 RESUME. ✓
- Liveness captured (lost today) → Task 2 `snapshot_deploy` reads `_drivers`. ✓
- Restore failure keeps deploy + flags sessions → Task 4 (warning, no rollback) + Task 6 (UI). ✓
- Scrollback bound to replay buffer / unsent input → **already covered by transcript + localStorage**; explicitly NOT re-built (Task 7 step 4 guards against it). ✓
- RESUME skipped for edge-only → Task 4 (`daemon_changed` gate). ✓

**Placeholder scan:** none — every step has runnable code + concrete commands. `<main-checkout>` is the one literal the implementer substitutes (the prebuilt-env path, per Global Constraints).

**Type consistency:** `snapshot_deploy()->{"live":[{"id","status"}]}` and `restore_deploy()->{"restored":[],"failed":[]}` match the controller's `self.status.restore` shape, `_deploy_dict()["restore"]`, the TS `DeployProgress.restore`, and the DeployPanel render. `ensure_driver(session_id, identity)` signature matches the real orchestrator method. ✓

**Cross-plan note:** This completes the SAVE/RESUME phases that Plans 1–2 left as no-ops. After Plan 3, all five phases in the `Phase` enum are driven, and the DeployPanel checklist reflects real snapshot/restore work.
