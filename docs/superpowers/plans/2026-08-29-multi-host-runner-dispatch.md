# Multi-host runner dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route each new `claude` session to the best fleet host (auto-ranked, with a manual override) and route every per-session operation to the host that owns it, so `dobby` can run sessions alongside `rocky`.

**Architecture:** Introduce `FleetOrchestrator`, a host-aware facade that becomes `app.state.sessions` in remote mode. It wraps the existing `FleetRouter` (one `RemoteOrchestrator` per enabled `Host` row). It routes: `create_session` → ranked `pick()` + stamp the chosen host; per-session methods → the owning host (from `session.host`, cached); batched `*_many` enrichment → fan out by host and merge; everything else → the primary host (the `CCSWITCH_SESSIOND_ENDPOINT` host). It preserves `@rpc_deadline` markers so `call_with_deadline` hands deadlines to httpx instead of cancelling sockets (#734). Onboarding a runner is captured as a runbook; `dobby` is then enabled via mTLS with a session cap.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy(async) / httpx (mTLS) · React+TS+Vite (web) · systemd + mTLS on the hosts.

**Spec:** `docs/superpowers/specs/2026-08-29-multi-host-runner-dispatch-design.md` (detailed design for sub-project 4 of `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md`).

## Global Constraints

- **No in-process fallback in remote mode.** When no host is dispatchable, raise `NoHostAvailable` — never run in-process over the shared DB (split-brain guard; `deploy/ccswitch.service.d/remote.conf`, the 2026-06-23 "Ready while working" incident).
- **Preserve `@rpc_deadline`.** Any routing wrapper exposed as an `app.state.sessions` method that is marked on `RemoteOrchestrator` MUST carry `__ccswitch_rpc_deadline__ = True`, or `call_with_deadline` falls back to `asyncio.wait_for`, cancels the httpx request, and re-triggers the #734 socket leak. Marked methods: `prs_for`, `prs_for_many`, `ensure_driver`, `latest_suggestions`, `latest_summary`, `latest_suggestions_many`, `latest_summary_many`, `usage`.
- **No new model or column.** `Host` and `Session.host` already exist. Adding neither → the Postgres-suite / reconcile-list obligations in CLAUDE.md stay N/A. If this changes, that whole obligation re-applies.
- **Single source of truth (Principle VI).** Delete the static host stamp (`server/api/sessions.py:580-595`) in the same change that moves stamping into `FleetOrchestrator`. Do not leave two stamping paths.
- **Gate before push (four steps + web):** `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`; then in `web/`: `vitest run`, `tsc --noEmit`, `eslint`.
- **Test env:** run `server/tests` with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/…`, and unset `CCSWITCH_SESSIOND_*` locally (module-level `create_app()` demands client certs otherwise). A fresh worktree has no built env — use the main checkout's `.venv` and symlink `web/node_modules`.

---

### Task 1: `FleetOrchestrator` skeleton — default routing to primary + marker passthrough

**Files:**
- Create: `server/sessions/fleet_orchestrator.py`
- Test: `server/tests/test_fleet_orchestrator.py`

**Interfaces:**
- Consumes: `FleetRouter` (`server/sessions/fleet_router.py`) — `.get(name) -> orch`, `.pick(prefer=None) -> list[str]`, `.names() -> list[str]`; `_DEADLINE_ATTR` from `server/sessions/facade.py`.
- Produces: `class FleetOrchestrator(router: FleetRouter, primary: str)`. Attribute access for any name **not** explicitly overridden returns the primary host's real bound method (sync/async and `@rpc_deadline` marker intact, because it *is* the real method). Explicit overrides land in Tasks 2-4.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_orchestrator.py
import pytest
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.facade import _DEADLINE_ATTR


class _FakeOrch:
    def __init__(self, name): self.name = name; self.calls = []
    async def list_sessions(self): self.calls.append("list_sessions"); return [self.name]
    async def usage(self, acct, *, timeout=None):  # mirrors a @rpc_deadline method
        self.calls.append(("usage", acct, timeout)); return {"acct": acct}
    usage.__ccswitch_rpc_deadline__ = True
    def daemon_sha(self): return "abc1234"


class _FakeRouter:
    def __init__(self, orchs): self._orchs = orchs
    def get(self, name): return self._orchs[name]
    def names(self): return sorted(self._orchs)
    async def pick(self, *, prefer=None): return sorted(self._orchs)


@pytest.mark.asyncio
async def test_unrouted_method_goes_to_primary():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert await fo.list_sessions() == ["rocky"]
    assert prim.calls == ["list_sessions"]


@pytest.mark.asyncio
async def test_primary_method_preserves_rpc_deadline_marker():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    # The exposed callable must still be recognised as native-deadline-capable,
    # or call_with_deadline cancels the socket (#734).
    assert getattr(fo.usage, _DEADLINE_ATTR, False) is True


def test_sync_cached_read_passes_through():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert fo.daemon_sha() == "abc1234"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError: server.sessions.fleet_orchestrator`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/fleet_orchestrator.py
"""Host-aware orchestrator facade (spec: 2026-08-29-multi-host-runner-dispatch-design).

Becomes app.state.sessions in remote mode. Routes each edge->session call to the
right daemon; unrouted calls go to the primary host's REAL bound method, so sync/
async behaviour and the @rpc_deadline marker are preserved for free.
"""
from server.sessions.fleet_router import FleetRouter


class FleetOrchestrator:
    def __init__(self, router: FleetRouter, primary: str) -> None:
        self._router = router
        self._primary = primary
        self._host_cache: dict[str, str] = {}  # session_id -> host (populated in Task 2/3)

    def _primary_orch(self):
        return self._router.get(self._primary)

    def __getattr__(self, name: str):
        # Explicit overrides (create_session, per-session, fan-out, subscribe) are real
        # methods and never reach __getattr__. Everything else = the primary's own bound
        # method: correct sync/async, and the @rpc_deadline marker travels with it.
        return getattr(self._primary_orch(), name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_orchestrator.py server/tests/test_fleet_orchestrator.py
git commit -m "feat(fleet): FleetOrchestrator skeleton routing unmatched calls to primary"
```

---

### Task 2: Per-session routing by owning host

**Files:**
- Modify: `server/sessions/fleet_orchestrator.py`
- Test: `server/tests/test_fleet_orchestrator.py`

**Interfaces:**
- Consumes: shared DB `Session.host` (`server/db/models.py`), `server/db` async `db_session()` context manager (match the import style already used in `server/sessions/fleet_router.py`).
- Produces: per-session methods route to `session.host`'s orchestrator. `_PER_SESSION: frozenset[str]` (method names whose first positional arg is `session_id`). `async def _host_for(self, session_id) -> str | None`. Marked per-session methods (`ensure_driver`, `latest_suggestions`, `latest_summary`, `prs_for`) keep their `@rpc_deadline` marker. Explicit `def subscribe(self, session_id)` (sync).

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_fleet_orchestrator.py
@pytest.mark.asyncio
async def test_per_session_op_routes_to_owning_host(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")

    async def get_session(self, sid):  # per-session method on the fakes
        self.calls.append(("get_session", sid)); return {"id": sid, "host": self.name}
    _FakeOrch.get_session = get_session

    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky, "dobby": dobby}), primary="rocky")

    async def fake_host_for(sid):  # stand in for the DB lookup
        return {"s-on-dobby": "dobby", "s-on-rocky": "rocky"}.get(sid)
    monkeypatch.setattr(fo, "_host_for", fake_host_for)

    await fo.get_session("s-on-dobby")
    await fo.get_session("s-on-rocky")
    assert ("get_session", "s-on-dobby") in dobby.calls
    assert ("get_session", "s-on-rocky") in rocky.calls


@pytest.mark.asyncio
async def test_per_session_marked_method_keeps_deadline_marker():
    fo = FleetOrchestrator(_FakeRouter({"rocky": _FakeOrch("rocky")}), primary="rocky")
    assert getattr(fo.ensure_driver, _DEADLINE_ATTR, False) is True


@pytest.mark.asyncio
async def test_unknown_host_falls_back_to_primary(monkeypatch):
    rocky = _FakeOrch("rocky")

    async def get_session(self, sid): self.calls.append(("get_session", sid)); return {}
    _FakeOrch.get_session = get_session
    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky}), primary="rocky")
    monkeypatch.setattr(fo, "_host_for", lambda sid: _acoro(None))
    await fo.get_session("orphan")
    assert ("get_session", "orphan") in rocky.calls


async def _acoro(v): return v
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -k per_session -v`
Expected: FAIL — `get_session` currently routes to primary only (dobby never called), and `fo.ensure_driver` has no marker.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/fleet_orchestrator.py — add imports + members
from server.sessions.facade import _DEADLINE_ATTR
from server.sessions.remote import RemoteOrchestrator

# Method names whose FIRST positional arg is session_id and that touch live daemon
# state (must run on the owning host). Pinned by a completeness test (Task 4a).
_PER_SESSION: frozenset[str] = frozenset({
    "get_session", "send_prompt", "ensure_driver", "session_queue_move",
    "session_queue_cancel", "cancel_queued", "archive_session", "permanent_delete",
    "rename_session", "note_user_interaction", "set_autopilot_paused", "interrupt",
    "set_permission_mode", "resolve_permission", "answer_question", "pending_questions",
    "pending_permissions", "transcript_path", "transcript_events",
    "resolve_transcript_path", "transcript_window", "transcript_search_text",
    "command_catalog", "run_command", "latest_suggestions", "generate_suggestions_now",
    "latest_summary", "pop_pending_initial_prompt", "pop_pending_initial_attachment_ids",
    "request_pr_fix", "prs_for", "ring_covers", "ring_tail", "unarchive",
    "x11_open_browser",
})

    async def _host_for(self, session_id: str) -> str | None:
        cached = self._host_cache.get(session_id)
        if cached is not None:
            return cached
        from sqlalchemy import select as _sel
        from server.db import db_session
        from server.db.models import Session as _S
        async with db_session() as db:
            host = (await db.execute(
                _sel(_S.host).where(_S.id == session_id))).scalar_one_or_none()
        if host:
            self._host_cache[session_id] = host
        return host

    def _orch_for_session_sync(self, session_id: str):
        host = self._host_cache.get(session_id)
        try:
            return self._router.get(host) if host else self._primary_orch()
        except Exception:
            return self._primary_orch()

    def _make_per_session(self, name: str):
        async def _call(session_id, *a, **k):
            host = await self._host_for(session_id)
            try:
                orch = self._router.get(host) if host else self._primary_orch()
            except Exception:
                orch = self._primary_orch()
            return await getattr(orch, name)(session_id, *a, **k)
        if getattr(getattr(RemoteOrchestrator, name, None), _DEADLINE_ATTR, False):
            setattr(_call, _DEADLINE_ATTR, True)
        return _call

    def subscribe(self, session_id: str):
        # Sync per-session iterator. The WS handler calls ensure_driver (async, warms
        # the cache) before subscribe, so the cache is populated by now; on a miss we
        # fall back to primary rather than block on a DB read from a sync method.
        return self._orch_for_session_sync(session_id).subscribe(session_id)
```

Update `__getattr__` to dispatch per-session names first:

```python
    def __getattr__(self, name: str):
        if name in _PER_SESSION:
            return self._make_per_session(name)
        return getattr(self._primary_orch(), name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_orchestrator.py server/tests/test_fleet_orchestrator.py
git commit -m "feat(fleet): route per-session ops to the owning host"
```

---

### Task 3: `create_session` — ranked pick, cascade, stamp chosen host

**Files:**
- Modify: `server/sessions/fleet_orchestrator.py`
- Test: `server/tests/test_fleet_orchestrator.py`

**Interfaces:**
- Consumes: `FleetRouter.pick(prefer=None) -> list[str]`, `NoHostAvailable` (`server/sessions/fleet_router.py`).
- Produces: `async def create_session(self, *, prefer: str | None = None, **kwargs) -> SessionRecord`. Pops `prefer`, ranks hosts, spawns on the first that succeeds, caches + **stamps** `Session.host` for the returned record, cascades on spawn error, raises the last spawn error (or `NoHostAvailable` when the ranked list is empty). `record.host` is set on the returned object.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_fleet_orchestrator.py
class _Rec:
    def __init__(self, id): self.id = id; self.host = None

@pytest.mark.asyncio
async def test_create_picks_top_host_and_stamps(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec(f"sess-{self.name}")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby})
    async def pick(*, prefer=None): return ["dobby", "rocky"]  # dobby ranked first
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    stamped = {}
    async def fake_stamp(sid, host): stamped[sid] = host
    monkeypatch.setattr(fo, "_stamp_host", fake_stamp)

    rec = await fo.create_session(account="a")
    assert rec.id == "sess-dobby" and rec.host == "dobby"
    assert stamped == {"sess-dobby": "dobby"}
    assert fo._host_cache["sess-dobby"] == "dobby"

@pytest.mark.asyncio
async def test_create_cascades_on_spawn_failure(monkeypatch):
    good = _FakeOrch("rocky")
    bad = _FakeOrch("dobby")
    async def ok(self, **kw): return _Rec("s1")
    async def boom(self, **kw): raise RuntimeError("spawn failed")
    bad.create_session = boom.__get__(bad); good.create_session = ok.__get__(good)
    router = _FakeRouter({"rocky": good, "dobby": bad})
    async def pick(*, prefer=None): return ["dobby", "rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    rec = await fo.create_session()
    assert rec.id == "s1" and rec.host == "rocky"

@pytest.mark.asyncio
async def test_prefer_is_forwarded_not_passed_to_daemon(monkeypatch):
    rocky = _FakeOrch("rocky"); seen = {}
    async def create_session(self, **kw): seen.update(kw); return _Rec("s1")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky}); got = {}
    async def pick(*, prefer=None): got["prefer"] = prefer; return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(prefer="rocky", account="a")
    assert got["prefer"] == "rocky"
    assert "prefer" not in seen and seen["account"] == "a"  # prefer never reaches daemon
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -k create -v`
Expected: FAIL — `create_session` currently resolves via `__getattr__` to primary, ignores `prefer`, does not stamp.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/fleet_orchestrator.py — add explicit method
    async def create_session(self, *, prefer: str | None = None, **kwargs):
        order = await self._router.pick(prefer=prefer)  # raises NoHostAvailable if empty
        last_exc: Exception | None = None
        for host in order:
            try:
                rec = await self._router.get(host).create_session(**kwargs)
            except Exception as e:  # spawn/clone/account errors → try next ranked host
                last_exc = e
                continue
            self._host_cache[rec.id] = host
            await self._stamp_host(rec.id, host)
            try:
                rec.host = host
            except Exception:  # pragma: no cover — frozen record types
                pass
            return rec
        raise last_exc if last_exc is not None else NoHostAvailable("no host accepted the session")

    async def _stamp_host(self, session_id: str, host: str) -> None:
        from sqlalchemy import update as _upd
        from server.db import db_session
        from server.db.models import Session as _S
        async with db_session() as db:
            await db.execute(_upd(_S).where(_S.id == session_id).values(host=host))
            await db.commit()
```

Add the import: `from server.sessions.fleet_router import FleetRouter, NoHostAvailable`.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_orchestrator.py server/tests/test_fleet_orchestrator.py
git commit -m "feat(fleet): create_session ranks, cascades, and stamps the chosen host"
```

---

### Task 4: Fan-out for batched enrichment (`*_many`)

**Files:**
- Modify: `server/sessions/fleet_orchestrator.py`
- Test: `server/tests/test_fleet_orchestrator.py`

**Interfaces:**
- Produces: `_FANOUT_BY_IDS: frozenset[str] = {"latest_suggestions_many","latest_summary_many","prs_for_many"}`. Each groups its `session_ids` by owning host, calls each host's method with only its subset concurrently, and merges the returned dicts. Keeps the `@rpc_deadline` marker and forwards `timeout=`.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_fleet_orchestrator.py
@pytest.mark.asyncio
async def test_fanout_groups_ids_by_host_and_merges(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def many(self, ids, *, timeout=None, viewing=False):
        return {i: f"{self.name}:{i}" for i in ids}
    rocky.latest_summary_many = many.__get__(rocky)
    dobby.latest_summary_many = many.__get__(dobby)
    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky, "dobby": dobby}), primary="rocky")
    async def host_for(sid): return "dobby" if sid.startswith("d") else "rocky"
    monkeypatch.setattr(fo, "_host_for", host_for)
    out = await fo.latest_summary_many(["r1", "d1", "r2"], timeout=1.0)
    assert out == {"r1": "rocky:r1", "d1": "dobby:d1", "r2": "rocky:r2"}

@pytest.mark.asyncio
async def test_fanout_keeps_deadline_marker():
    fo = FleetOrchestrator(_FakeRouter({"rocky": _FakeOrch("rocky")}), primary="rocky")
    assert getattr(fo.latest_summary_many, _DEADLINE_ATTR, False) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -k fanout -v`
Expected: FAIL — `latest_summary_many` routes to primary, so `d1` gets `rocky:d1`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/fleet_orchestrator.py
import asyncio

_FANOUT_BY_IDS: frozenset[str] = frozenset({
    "latest_suggestions_many", "latest_summary_many", "prs_for_many",
})

    def _make_fanout(self, name: str):
        async def _call(session_ids, *a, **k):
            groups: dict[str, list[str]] = {}
            for sid in session_ids:
                host = await self._host_for(sid) or self._primary
                groups.setdefault(host, []).append(sid)

            async def _one(host, subset):
                try:
                    orch = self._router.get(host)
                except Exception:
                    orch = self._primary_orch()
                return await getattr(orch, name)(subset, *a, **k)

            merged: dict = {}
            for res in await asyncio.gather(*(_one(h, s) for h, s in groups.items())):
                merged.update(res or {})
            return merged
        if getattr(getattr(RemoteOrchestrator, name, None), _DEADLINE_ATTR, False):
            setattr(_call, _DEADLINE_ATTR, True)
        return _call
```

Extend `__getattr__`:

```python
    def __getattr__(self, name: str):
        if name in _FANOUT_BY_IDS:
            return self._make_fanout(name)
        if name in _PER_SESSION:
            return self._make_per_session(name)
        return getattr(self._primary_orch(), name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_orchestrator.py server/tests/test_fleet_orchestrator.py
git commit -m "feat(fleet): fan out batched *_many enrichment by owning host"
```

---

### Task 4a: Routing-classification completeness guard

**Files:**
- Test: `server/tests/test_fleet_orchestrator.py`

**Interfaces:**
- Consumes: `RemoteOrchestrator` public method names; `_PER_SESSION`, `_FANOUT_BY_IDS`, and an explicit `_EXPLICIT`/`_GLOBAL` accounting in the test.
- Produces: a test that fails when a new `RemoteOrchestrator` method is added without being classified — preventing silent mis-routing (mirrors the `test_ipc_cancel_does_not_strand` allowlist pattern).

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_fleet_orchestrator.py
def test_every_remote_method_is_classified():
    import inspect
    from server.sessions.remote import RemoteOrchestrator
    from server.sessions.fleet_orchestrator import _PER_SESSION, _FANOUT_BY_IDS

    EXPLICIT = {"create_session", "subscribe", "aclose", "__init__"}
    # Global/primary-routed: everything not per-session, fan-out, or explicit. Listed
    # so adding a NEW method forces a conscious classification here.
    GLOBAL = {
        "list_sessions", "list_archived", "pr_for", "prs_for", "all_prs",
        "all_repos_stage_models", "all_merge_trains", "latest_queue_entries",
        "check_insights", "ready_merge", "deploy_config_list", "deploy_config_put",
        "deploy_status", "git_credential_alerts", "dismiss_git_credential_alert",
        "automation_statuses", "automation_actionable", "automation_trigger",
        "pr_doctor_job", "resume_recovery_status", "consolidation_candidates",
        "automation_consolidate", "backlog_candidates", "backlog_runner_dispatch",
        "list_adoptable_candidates", "migrate_archive_home", "cloned_repos", "usage",
        "active_session_count", "live_session_load", "live_session_counts",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel", "copilot_usage",
        "subscribe_events", "unsubscribe_events", "x11_metrics", "adopt_session",
        "daemon_sha", "daemon_pool_stats", "daemon_epoch",
    }
    public = {n for n, _ in inspect.getmembers(RemoteOrchestrator, callable)
              if not n.startswith("_")}
    classified = _PER_SESSION | _FANOUT_BY_IDS | EXPLICIT | GLOBAL
    unclassified = public - classified
    assert not unclassified, f"unclassified RemoteOrchestrator methods: {sorted(unclassified)}"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator.py -k classified -v`
Expected: PASS if the sets are complete; if it FAILS, it lists the unclassified names — add each to the correct set in `fleet_orchestrator.py` (per-session vs fan-out) or to `GLOBAL`/`EXPLICIT` in the test, then re-run. Do not silence it by adding to `GLOBAL` a method that clearly takes `session_id`.

- [ ] **Step 3: Commit**

```bash
git add server/tests/test_fleet_orchestrator.py
git commit -m "test(fleet): guard that every orchestrator method is classified for routing"
```

---

### Task 5: Wire `FleetOrchestrator` into `app.py` + populate the router; delete the static stamp

**Files:**
- Modify: `server/app.py:480-492` (remote-mode branch) and the fleet-init block near `server/app.py:271-292`.
- Modify: `server/api/sessions.py` — pass `host=body.host` (Task 6 adds the field; here pass `getattr(body, "host", None)`) into `create_session` as `prefer=`, and **delete** the static stamp at `server/api/sessions.py:580-595`.
- Test: `server/tests/test_fleet_orchestrator_wiring.py`

**Interfaces:**
- Consumes: `Host` registry rows, `DaemonClient(endpoint, client_tls=...)` (`server/ipc/client.py`), `RemoteOrchestrator` (`server/sessions/remote.py`), `client_tls_files()` (`server/ipc/tls.py`), `parse_endpoint` (`server/ipc/paths.py`).
- Produces: in remote mode, `app.state.sessions` is a `FleetOrchestrator` whose router holds one `RemoteOrchestrator(DaemonClient(row.endpoint))` per **enabled** `Host` row (including the primary). `async def build_fleet_router(primary_name, primary_orch) -> FleetRouter` in `fleet_orchestrator.py` (lazy reconcile helper reused at pick time is acceptable to defer to a follow-up; startup population is required now).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_orchestrator_wiring.py
import pytest
from server.sessions.fleet_orchestrator import build_fleet_router

@pytest.mark.asyncio
async def test_router_holds_a_client_per_enabled_host(monkeypatch, tmp_path):
    # Seed two enabled hosts + one disabled in a temp DB; build_fleet_router should
    # register orchestrators for the two enabled ones and skip the disabled one.
    from server.db import db_session, init_db
    from server.db.models import Host
    monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    await init_db()
    async with db_session() as db:
        db.add(Host(name="rocky", endpoint="https://rocky.ts.net:9443", enabled=True))
        db.add(Host(name="dobby", endpoint="https://dobby.ts.net:9443", enabled=True))
        db.add(Host(name="old", endpoint="https://old.ts.net:9443", enabled=False))
        await db.commit()

    built = []
    class _FakeOrch:
        def __init__(self, ep): built.append(ep)
    monkeypatch.setattr("server.sessions.fleet_orchestrator._orch_for_endpoint",
                        lambda ep: _FakeOrch(ep))
    router = await build_fleet_router(primary_name="rocky",
                                      primary_orch=_FakeOrch("primary"))
    assert set(router.names()) == {"rocky", "dobby"}  # 'old' disabled → skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator_wiring.py -v`
Expected: FAIL — `build_fleet_router` / `_orch_for_endpoint` do not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/fleet_orchestrator.py
def _orch_for_endpoint(endpoint: str):
    from server.ipc.client import DaemonClient
    return RemoteOrchestrator(DaemonClient(endpoint))

async def build_fleet_router(primary_name: str, primary_orch) -> FleetRouter:
    """One orchestrator per enabled Host row. The primary reuses the already-built
    orchestrator (its client is live); other hosts get a fresh DaemonClient using the
    edge's client TLS (one CA signs every daemon cert)."""
    from sqlalchemy import select as _sel
    from server.db import db_session
    from server.db.models import Host
    router = FleetRouter()
    router.add(primary_name, primary_orch)
    async with db_session() as db:
        rows = (await db.execute(_sel(Host).where(Host.enabled.is_(True)))).scalars().all()
    for r in rows:
        if r.name == primary_name:
            continue
        router.add(r.name, _orch_for_endpoint(r.endpoint))
    return router
```

Wire into `server/app.py` remote branch (replace line 492 `app.state.sessions = RemoteOrchestrator(...)`):

```python
        _primary = RemoteOrchestrator(DaemonClient(endpoint))
        app.state.remote_primary = _primary
        # app.state.host_name (resolved from endpoint in the fleet-init block) names the
        # primary host. Build the router after ensure_default_host has run.
```

In the fleet-init block (after `app.state.host_name = await ensure_default_host(...)`, ~`server/app.py:284`), add:

```python
        from server.sessions.fleet_orchestrator import FleetOrchestrator, build_fleet_router
        _primary_name = app.state.host_name or "local"
        _router = await build_fleet_router(_primary_name, app.state.remote_primary)
        app.state.sessions = FleetOrchestrator(_router, primary=_primary_name)
```

Delete `server/api/sessions.py:580-595` (the static `app.state.host_name` stamp — `FleetOrchestrator.create_session` now stamps). Pass the preference in the create call (around `server/api/sessions.py:507-519`):

```python
        record = await orch.create_session(
            prefer=getattr(body, "host", None),
            name=name, account=body.account, initial_prompt=body.initialPrompt,
            cwd=None, branch=body.branch, repo=body.repo, identity=identity,
            permission_mode=_resolved_permission_mode, attachment_ids=body.attachmentIds,
            name_is_auto=name_is_auto, model=_resolved_model,
        )
```

- [ ] **Step 4: Run tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_fleet_orchestrator_wiring.py server/tests/test_fleet_orchestrator.py -v`
Expected: PASS. Then run the existing session/create suites to confirm no regression: `... pytest server/tests/ -k "session or create or host or fleet" -q`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_orchestrator.py server/app.py server/api/sessions.py server/tests/test_fleet_orchestrator_wiring.py
git commit -m "feat(fleet): wire FleetOrchestrator as app.state.sessions; stamp host at create"
```

---

### Task 6: Manual override — `host` field in the create API + Runner dropdown in the UI

**Files:**
- Modify: `server/api/sessions.py:66-76` (`CreateSessionRequest`).
- Modify: `web/src/components/DispatchBar.tsx` (add a "Runner" select + include `host` in the dispatch payload) and its call site in `web/src/App.tsx` (thread the fleet-hosts list + payload field).
- Modify: `web/src/api/types.ts` (create-request payload type: add `host?: string | null`).
- Test: `server/tests/test_sessions_create_host_prefer.py`; `web/src/components/__tests__/DispatchBar.test.tsx`.

**Interfaces:**
- Consumes: `GET /api/fleet/hosts` (`server/api/fleet.py`) for the dropdown options (enabled hosts). `FleetOrchestrator.create_session(prefer=...)` (Task 3/5).
- Produces: `CreateSessionRequest.host: str | None = None`; the POST body forwards it as `prefer`. UI dropdown defaults to `''` = Auto (mirrors the existing `account` select convention at `DispatchBar.tsx:85`).

- [ ] **Step 1 (backend): Write the failing test**

```python
# server/tests/test_sessions_create_host_prefer.py — assert body.host reaches create_session as prefer
# Use the app test client with a fake orchestrator recording create_session kwargs.
def test_create_forwards_host_as_prefer(client_with_fake_orch):
    fake = client_with_fake_orch.app.state.sessions
    client_with_fake_orch.post("/api/sessions", json={"initialPrompt": "hi", "host": "dobby"})
    assert fake.last_create_kwargs["prefer"] == "dobby"
```
(Model the fixture on the existing session-create tests in `server/tests/`; reuse their app/orch doubles rather than inventing a new harness.)

- [ ] **Step 2: Run — expect FAIL** (`CreateSessionRequest` has no `host`; `prefer` is `None`).

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_sessions_create_host_prefer.py -v`

- [ ] **Step 3 (backend): Implement**

```python
# server/api/sessions.py — CreateSessionRequest
    model: str | None = None
    # Optional runner preference. None → auto-rank; a name → that host first, still
    # failing over. Forwarded to FleetOrchestrator.create_session as prefer=.
    host: str | None = None
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5 (frontend): Write the failing test**

```tsx
// web/src/components/__tests__/DispatchBar.test.tsx
it('includes the selected runner host in the dispatch payload', async () => {
  const onDispatch = vi.fn()
  render(<DispatchBar {...baseProps} hosts={[{name:'rocky'},{name:'dobby'}]} onDispatch={onDispatch} />)
  // select dobby in the Runner dropdown, type a prompt, submit
  fireEvent.change(screen.getByLabelText(/runner/i), { target: { value: 'dobby' } })
  fireEvent.change(screen.getByPlaceholderText(/./), { target: { value: 'do a thing' } })
  fireEvent.submit(screen.getByRole('form'))
  expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({ host: 'dobby' }))
})
```
(Match the existing `DispatchBar.test.tsx` render helpers/prop names; `hosts` is a new prop.)

- [ ] **Step 6: Run — expect FAIL** (`vitest run DispatchBar` in `web/`).

- [ ] **Step 7 (frontend): Implement**

- Add a `hosts?: {name: string}[]` prop and a `const [host, setHost] = useState('')` (`'' = Auto`).
- Render a "Runner" `<select>` beside account/model using the shared select styling noted at `DispatchBar.tsx:253`; options: `Auto` (`''`) + one per host.
- Include `host: host || null` in the `onDispatch({...})` payload (near `DispatchBar.tsx:144`), and in the POST body / create-request type in `web/src/api/types.ts`.
- In `web/src/App.tsx`, fetch `GET /api/fleet/hosts` (reuse any existing fleet fetch; else add one), pass enabled hosts as `hosts`, and include `host` in the create POST body.

- [ ] **Step 8: Run — expect PASS** (`vitest run`, `tsc --noEmit`, `eslint`).

- [ ] **Step 9: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_create_host_prefer.py web/src/
git commit -m "feat(fleet): manual runner override — host field + Runner dropdown"
```

---

### Task 7: Runner onboarding runbook ("standard work")

**Files:**
- Create: `docs/runbooks/onboarding-a-session-runner.md`

**Interfaces:**
- Consumes: the verified prerequisites in the spec ("Runner onboarding — the standard work").
- Produces: a copy-paste runbook + checklist that turns spec §"Runner onboarding" into ordered, verifiable steps, plus the dobby-specific application (missing server cert, `daemon.env` collision fix).

- [ ] **Step 1: Write the runbook** — reproduce the spec's 8 numbered onboarding steps verbatim as a checklist, each with the exact command where one exists (`scripts/ccswitch-mint-mtls.sh` for the cert with the SAN warning; the `pg_hba` `/32` + tailnet-listener note; the `/etc/ccswitch/daemon.env` template with `CCSWITCH_DB_URL`, matching `CCSWITCH_MASTER_KEY`, `CCSWITCH_SESSIOND_ENDPOINT=https://<host>.tail0dd570.ts.net:9443`, `CCSWITCH_SESSIOND_TLS_CERT/KEY/CA`, `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge`, `CCSWITCH_ENABLE_XVFB=true`; the account-store mount modelled on `ccswitch-mounts.service`; `systemctl enable --now ccswitch-daemon.service`; fleet registration via Fleet UI or `CCSWITCH_FLEET_HOSTS` with a `max_sessions` cap; and the verify step: probe `healthy=true`, matching `sha`/`protocol`, then a `host=<new>` smoke-test session lands there). Add a "Dobby specifics" section: mint the missing **server** cert (SAN `DNS:dobby.tail0dd570.ts.net,IP:100.102.11.121`), the dedicated `daemon.env` to avoid the edge-target/daemon-bind endpoint collision, and the already-satisfied items (local Postgres, `CCSWITCH_MASTER_KEY`, account store is local, verify Xvfb).

- [ ] **Step 2: Cross-check** the runbook against `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md` decisions 4-7 (shared creds, tailnet transport, cleartext-PG rationale) so it does not contradict the umbrella. Link both specs at the top.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/onboarding-a-session-runner.md
git commit -m "docs: runbook for onboarding a ccswitch session runner"
```

---

### Task 8: Full gate + open PR

- [ ] **Step 1: Run the full gate.**

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest tests/
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/
cd web && npx vitest run && npx tsc --noEmit && npx eslint . ; cd ..
```
Expected: all green. Fix anything red before proceeding (mypy in particular — e.g. the router/orch `object` typing may need `RemoteOrchestrator | InProcessOrchestratorAdapter` hints).

- [ ] **Step 2: Push and open a non-draft PR with auto-merge** (per CLAUDE.md completion workflow; branch already off `origin/master`).

```bash
git push -u origin feat/multi-host-runner-dispatch
gh pr create --title "feat(fleet): multi-host runner dispatch + onboarding runbook" \
  --body "$(cat <<'EOF'
Wires the existing (but dead) FleetRouter/ranking into session creation and per-session
routing so sessions can run on more than one host. See
docs/superpowers/specs/2026-08-29-multi-host-runner-dispatch-design.md.

- FleetOrchestrator: create → ranked pick + stamp chosen host; per-session ops → owning
  host; batched *_many → fan out + merge; everything else → primary. @rpc_deadline markers
  preserved (#734 guard). Classification completeness test.
- Manual runner override (host field + Runner dropdown).
- No new model/column; single-host degrades to today's behaviour (rollback-safe).
- Onboarding runbook (docs/runbooks/onboarding-a-session-runner.md).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

---

### Task 9 (OPS — after merge, not part of the code PR): enable dobby as a runner

Run from the runbook. **Not** a code change — performed by the operator/me on production after the PR lands. Sequenced per the user's choice ("spec first, then code, then I run ops").

- [ ] **Step 1:** Mint dobby's **server** cert: `bash scripts/ccswitch-mint-mtls.sh` for SAN `DNS:dobby.tail0dd570.ts.net,IP:100.102.11.121`; install cert/key + CA under `/etc/ccswitch/tls/` on dobby.
- [ ] **Step 2:** Write `/etc/ccswitch/daemon.env` on dobby (server certs + `CCSWITCH_SESSIOND_ENDPOINT=https://dobby.tail0dd570.ts.net:9443` + `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge` + shared `CCSWITCH_MASTER_KEY`/`CCSWITCH_DB_URL` + `CCSWITCH_ENABLE_XVFB=true`); point dobby's `ccswitch-daemon.service` `EnvironmentFile` at it (drop-in), so it does not inherit the edge's `CCSWITCH_SESSIOND_ENDPOINT=https://rocky…`.
- [ ] **Step 3:** `sudo systemctl enable --now ccswitch-daemon.service` on dobby; confirm it binds `:9443` and answers `/info`.
- [ ] **Step 4:** Register dobby in the fleet (Fleet UI `POST /hosts` → `dobby` / `https://dobby.tail0dd570.ts.net:9443`, or add to `CCSWITCH_FLEET_HOSTS`), set `enabled=true` and `max_sessions=<cap>` (decide the cap — e.g. 3-4, leaving edge headroom on dobby's 16 cores).
- [ ] **Step 5:** Verify: `GET /api/fleet/hosts` shows dobby `healthy=true`, matching `sha`/`protocol`; create a smoke-test session with the Runner override set to `dobby` and confirm the `HostBadge` shows dobby and the PTY runs on dobby. Then leave auto-dispatch on to shed rocky's load.
- [ ] **Rollback:** disable/remove dobby in the Fleet UI → registry is single-host → `FleetOrchestrator` degrades to rocky-only, no restart needed.

---

## Self-Review

**Spec coverage:**
- Decision 1 (auto + override) → Tasks 3, 6. Decision 2 (cap) → Task 9 §4 + ranking's existing `_dispatchable`. Decision 3 (dobby https/mTLS + daemon.env) → Task 9 §1-2 + runbook. Decision 4 (defer locality) → not implemented, documented in spec (no task, intentional). Decision 5 (global ops pinned to primary) → Task 1 default routing + Task 4a `GLOBAL` set. Per-session routing → Task 2. Fan-out for shared-DB-invisible enrichment → Task 4. Router population/sync → Task 5. Protocol/health gating "no whole-edge hard-fail" → relies on existing `_dispatchable` (health/protocol/cap); **verify during Task 5** that a probe-marked incompatible host is simply excluded and does not break startup (add a wiring test if not already covered). Onboarding runbook → Task 7. Rollback → Task 9. Testing → each task + Task 8.
- **Gap noted:** the spec's "protocol mismatch → not dispatchable" is assumed to fall out of `_dispatchable`, but `_dispatchable` (`server/sessions/fleet.py:45`) checks enabled/healthy/cap, not `protocol`. If a stale-SHA/incompatible daemon can be `healthy=True`, add a protocol check to `candidates()`/`_dispatchable` — resolve in Task 5 with a test; do not assume.

**Placeholder scan:** `<host>`, `<new>`, `<cap>` are runbook template tokens (Tasks 7, 9), not code placeholders. No "TODO/implement later" in code steps.

**Type consistency:** `create_session(prefer=..., **kwargs)` (Task 3) matches the call site (Task 5) and the `host` field name (Task 6). `_PER_SESSION`/`_FANOUT_BY_IDS`/`build_fleet_router`/`_orch_for_endpoint` names are consistent across Tasks 2-5 and the completeness test (4a). `_host_for`/`_stamp_host`/`_host_cache` consistent across Tasks 2-4.
