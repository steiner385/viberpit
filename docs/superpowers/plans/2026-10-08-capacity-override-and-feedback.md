# Capacity: queue-don't-refuse, say why, and "Start now" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session (manual or automation-launched) that capacity (slots, product holds, or account quota) cannot start right now is *queued with a visible reason*, never silently refused, and any queued session can be force-started over slot caps and product holds with a per-session "Start now" override.

**Architecture:** Server: (1) the daemon RPC layer logs every handler exception so a refusal's reason is never lost; (2) `create_session` converts an `AllAccountsBusy(at_ceiling)` from a fresh manual spawn into a queued row with `queue_reason="account_limited"`, which the drain re-probes before starting; (3) `_maybe_start_queued(force=<id>)` starts one named entry first, skipping the global/host/product/runner gates, exposed as `POST /api/sessions/{id}/start-now` (audited). Web: the queue renders each session's `queueStallReason` plus a "Start now" button, a queued create toasts its reason, and the dock always says "all accounts limited" inline instead of relying on a banner that `AppShell` clears in the same render.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async (pytest, `asyncio.run` style), React + TS + React Query + sonner (vitest + Testing Library).

**Spec:** This document (requirements gathered 2026-10-08 from the user + the incident below). User decisions: the override bypasses **slot caps (global / host / product in-flight / runner) and product holds (e.g. PR-Doctor priority)** — **not** account usage ceilings. An account-limited create — manual **or automation** — **queues + explains** rather than refusing.

**Incident evidence (2026-10-08 10:21 ET, dobby):** `POST /api/sessions` → 409. Both daemons (`dobby`, `rocky`) created a worktree, rolled it back and returned `500` from `/rpc/create_session` with nothing logged (`server/ipc/daemon.py` `rpc()` swallows the exception into an envelope). `GET /api/capacity/binding` reported a KinDash PR-Doctor product hold. Two Claude accounts sit at 100% `weekly:fable`. `queueStallReason` is computed by `server/api/sessions.py` but rendered nowhere in `web/src`. `AppShell.tsx` clears the all-limited banner whenever any account has a non-null `rank`, which two accounts do, so the dock's only signal for `all_accounts_limited` is erased in the same commit.

## Global Constraints

- Branch is `fix/capacity-block-feedback` in worktree `.worktrees/capacity-override`, based on `origin/master`.
- Run Python with the main checkout's env: `/home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`; symlink `web/node_modules` from `/home/tony/GitHub/viberpit/web/node_modules` before any `npm run`.
- Before running `server/tests/`: `env -u VIBERPIT_SESSIOND_CERT -u VIBERPIT_SESSIOND_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST VIBERPIT_DEV=1` (unset every `VIBERPIT_SESSIOND_*`).
- Mixed-version fleet: rocky's daemon may lag. Never add a new key to the `session_queue.params` blob that `_spawn_session(**params)` would receive. `queue_reason` is the one already-popped key; reuse it.
- Never use `func.now()`; never hand-roll `task.cancel()`. Use `db_utc_now()` / `cancel_and_reap()`.
- `npm run lint` uses `--max-warnings 0`; run the **full** `npm run test` (many files mock `@/api/hooks`, so adding `useQueueStartNow` means adding it to those mocks).
- Commit messages via `git commit -F <file>`; trailer `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Copy: the user-facing button label is exactly **"Start now"**; its `aria-label` is ``Start ${name} now, over capacity limits``.

## Review Focus

1. **Start now on an `account_limited` entry while still limited** → must refuse with 409 `all_accounts_limited` + `earliestReset` and leave the session queued (not `ended`). Pinned in Task 3.
2. **Start now on a session another host owns** → must run on the owning daemon (fleet `_PER_SESSION` routing), not spawn a duplicate on the primary. Pinned in Task 3 (`test_start_queued_now_is_per_session_routed`).
3. **Automation dispatch (`origin` / `ownership_token` set) hits `AllAccountsBusy`** → queues as `account_limited` exactly like a manual create (user decision 2026-10-08); its claim/ownership must survive the queue → spawn hand-off the same way a `runner_capacity` entry's does. Pinned in Task 2.
4. **`no_usable_accounts` (re-auth needed)** → must still refuse with `accounts_need_reauth`; queueing behind a credential problem would wait forever. Pinned in Task 2.
5. **Old daemon drains an `account_limited` entry** → pops `queue_reason`, spawn raises `AllAccountsBusy`, the session is ended (documented degradation, no TypeError). Not testable in-tree; called out in the PR body.

---

### Task 1: Daemon RPC logs every handler exception

**Files:**
- Modify: `server/ipc/daemon.py` (the `rpc()` handler, ~line 333-350)
- Test: `server/tests/test_ipc_daemon_rpc.py`

**Interfaces:**
- Produces: log record from logger `server.ipc.daemon` with message prefix `rpc.error method=`.

- [ ] **Step 1: Write the failing test** (append to `server/tests/test_ipc_daemon_rpc.py`)

```python
def test_handler_exception_is_logged_with_type_and_message(caplog):
    import logging
    app = create_daemon_app()

    async def boom(params):
        raise ValueError("kaboom")

    register_rpc(app, "boom", boom)
    with caplog.at_level(logging.INFO, logger="server.ipc.daemon"):
        with TestClient(app) as c:
            c.post("/rpc/boom", json={})
    msgs = [r.getMessage() for r in caplog.records if r.name == "server.ipc.daemon"]
    assert any("rpc.error method=boom type=ValueError" in m and "kaboom" in m for m in msgs)


def test_typed_refusal_logged_at_info_not_warning(caplog):
    """AllAccountsBusy is an expected refusal: visible, but not an errsink WARNING."""
    import logging
    from viberpit.selector import AllAccountsBusy
    app = create_daemon_app()

    async def busy(params):
        raise AllAccountsBusy(None, "at_ceiling")

    register_rpc(app, "busy", busy)
    with caplog.at_level(logging.INFO, logger="server.ipc.daemon"):
        with TestClient(app) as c:
            c.post("/rpc/busy", json={})
    recs = [r for r in caplog.records if "rpc.error method=busy" in r.getMessage()]
    assert recs and all(r.levelno == logging.INFO for r in recs)
```

- [ ] **Step 2: Run to verify failure**

Run: `… pytest server/tests/test_ipc_daemon_rpc.py -k logged -v`
Expected: FAIL (no such log record).

- [ ] **Step 3: Implement.** In `server/ipc/daemon.py`, make sure a module logger exists (`log = logging.getLogger(__name__)`; reuse it if it's already there). Then, inside `except Exception as exc:` in `rpc()` and before building `err`:

```python
            # Every refusal used to leave only "500" in the journal (2026-10-08: a
            # create failed on both hosts and nothing said why). Typed refusals the
            # edge reconstructs (they carry error_data) are expected — INFO; anything
            # else is a handler defect — WARNING with the traceback.
            if getattr(exc, "error_data", None) is not None:
                log.info("rpc.error method=%s type=%s message=%s",
                         method, type(exc).__name__, exc)
            else:
                log.warning("rpc.error method=%s type=%s message=%s",
                            method, type(exc).__name__, exc, exc_info=True)
```

- [ ] **Step 4: Run** `… pytest server/tests/test_ipc_daemon_rpc.py -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `fix(ipc): log every daemon RPC handler exception with its type`.

---

### Task 2: An account-limited manual create queues as `account_limited`; the drain re-probes it

**Files:**
- Modify: `server/sessions/session_queue.py` (add `entry_for`)
- Modify: `server/sessions/orchestrator.py`: `create_session` (~3595-3620, the `_spawn_session` return); `_maybe_start_queued` (~4399, beside the `product_capacity` re-check); new helper `_account_headroom_or_raise`
- Test: create `server/tests/test_account_limited_queue.py`

**Interfaces:**
- Produces: `session_queue.entry_for(session_id: str) -> SessionQueue | None`
- Produces: `SessionOrchestrator._account_headroom_or_raise(params: dict) -> None`, which raises `AllAccountsBusy` while every account eligible for `params`' engine/model is at its ceiling.
- Produces: queue reason string `"account_limited"`.

- [ ] **Step 1: Write the failing tests** in `server/tests/test_account_limited_queue.py`

```python
"""A create (manual or automation) that finds every account at its ceiling is QUEUED
with queue_reason="account_limited" (user decision 2026-10-08), and the drain starts
it only once select_account finds headroom again."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from server.sessions import session_queue as sq
from server.tests.test_session_cap_enqueue import _make_orch, _stop_orch
from viberpit.selector import AllAccountsBusy

_RESET = datetime(2030, 1, 1, 10, 0, 0)


def _busy(*a, **k):
    raise AllAccountsBusy(_RESET, "at_ceiling")


def test_manual_create_at_ceiling_queues_account_limited(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'a.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"))
            assert rec.status == "queued"
            entry = await sq.entry_for(rec.id)
            assert entry is not None and entry.params.get("queue_reason") == "account_limited"
            assert rec.id not in orch._drivers
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_no_usable_accounts_still_refuses(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'b.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            def reauth(*a, **k):
                raise AllAccountsBusy(None, "no_usable_accounts")
            with patch("server.sessions.orchestrator.select_account", side_effect=reauth):
                with pytest.raises(AllAccountsBusy):
                    await orch.create_session("m", initial_prompt="hi",
                                              cwd=str(tmp_path / "reporoot"))
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_automation_launch_at_ceiling_also_queues(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"),
                                                origin="automation:test",
                                                origin_ref="acme/widgets#1")
            assert rec.status == "queued"
            entry = await sq.entry_for(rec.id)
            assert entry.params.get("queue_reason") == "account_limited"
            assert entry.params.get("origin") == "automation:test"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_drain_leaves_account_limited_queued_until_headroom(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"))
                assert await orch._maybe_start_queued() == 0
            assert orch.get_session(rec.id).status == "queued"
            # Headroom returns: the real (fixture-seeded) selector now picks "alpha".
            assert await orch._maybe_start_queued() == 1
            assert orch.get_session(rec.id).status != "queued"
            assert await sq.entry_for(rec.id) is None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())
```

> Automations queue too (user decision). For a create carrying an `ownership_token` (a Backlog Runner claim), first read how the existing `wait_for_slot` → `queue_reason="runner_capacity"` path enqueues such a claim. Look at what it does with `ownership_token` and whether `_maybe_start_queued` / `_automation_before_execution` find the claim again by `session_id`. Route `account_limited` through the identical mechanism so the claim is neither leaked nor double-counted. If that path does extra bookkeeping (e.g. stamping the operation as queued), do the same here. Add a test that queues an ownership-token create at the ceiling and asserts the claim/operation still resolves to this session after the drain starts it, modelled on the nearest existing `wait_for_slot` test (`grep -rln wait_for_slot server/tests`).

- [ ] **Step 2: Run** `… pytest server/tests/test_account_limited_queue.py -v`. Expected: FAIL (`entry_for` missing; create raises).

- [ ] **Step 3a: Add `entry_for`** to `server/sessions/session_queue.py`, following the module's existing `db_session()` + `select` style:

```python
async def entry_for(session_id: str) -> SessionQueue | None:
    """The queue entry for *session_id*, or None when it is not queued."""
    async with db_session() as db:
        return (await db.execute(
            select(SessionQueue).where(SessionQueue.session_id == session_id)
        )).scalar_one_or_none()
```

- [ ] **Step 3b: Probe helper** in `SessionOrchestrator`, next to `_maybe_start_queued`:

```python
    async def _account_headroom_or_raise(self, params: dict) -> None:
        """Raise AllAccountsBusy while no account could take this queued entry.

        A read-only probe (reserve=False): an availability check must never burn a
        reservation window. An entry pinned to an account skips the probe, since
        select_account is not what spawns it."""
        if params.get("account"):
            return
        engine = parse_agent_engine(params.get("engine"), legacy_default=True)
        await asyncio.to_thread(
            select_account, reserve=False,
            exclude=self._recovery.quarantined(now=self._clock()),
            engine=engine, model=quota_model(params.get("model"), engine=engine))
```

(`quota_model` is already used at the `_spawn_session` call site. Import it the same way if it isn't module-level.)

- [ ] **Step 3c: Queue instead of refuse.** In `create_session`, replace the final `return await self._spawn_session(...)` with a try that keeps the exact same kwargs:

```python
            try:
                return await self._spawn_session(
                    session_id, name=name, account=account, initial_prompt=initial_prompt,
                    cols=cols, rows=rows, cwd=cwd, branch=branch, repo=repo,
                    identity=identity, permission_mode=permission_mode,
                    attachment_ids=attachment_ids, name_is_auto=name_is_auto,
                    origin=origin, origin_ref=origin_ref, model=model,
                    reasoning_effort=reasoning_effort, kind=kind,
                    owner_identity=owner_identity, engine=selected_engine,
                    client_node=client_node, display=display, parent_session_id=parent_session_id,
                    autopilot_paused=autopilot_paused,
                    auto_archive_opt_out_reason=auto_archive_opt_out_reason)
            except AllAccountsBusy as busy:
                # Wait for quota rather than refuse — for people AND automations
                # (2026-10-08). Re-auth is not a wait (it never clears on its own), and
                # the assistant never queues (it is exempt from every cap).
                if busy.kind != "at_ceiling" or is_assistant_kind(kind):
                    raise
                log.info("session.create id=%s every account at its ceiling — queueing "
                         "(account_limited, earliest_reset=%s)", session_id, busy.earliest_reset)
                return await self._enqueue_session(
                    session_id, name=name, account=account,
                    initial_prompt=initial_prompt, cols=cols, rows=rows, cwd=cwd,
                    branch=branch, repo=repo, identity=identity,
                    permission_mode=permission_mode, attachment_ids=attachment_ids,
                    name_is_auto=name_is_auto, origin=origin, origin_ref=origin_ref,
                    model=model, reasoning_effort=reasoning_effort, kind=kind,
                    owner_identity=owner_identity, engine=selected_engine,
                    client_node=client_node, display=display, parent_session_id=parent_session_id,
                    autopilot_paused=autopilot_paused,
                    auto_archive_opt_out_reason=auto_archive_opt_out_reason,
                    queue_reason="account_limited")
```

`_spawn_session` already releases its worktree and display on a pre-driver raise (`_release_prespawn_resources`), so the queued row starts clean. Confirm `session_id` was not left in `self._records` as an active record after the raise; `_enqueue_session` overwrites it either way.

- [ ] **Step 3d: Drain re-probe.** In `_maybe_start_queued`, directly after the `runner_capacity` block and before `self._records.setdefault(sid, in_mem)`:

```python
                if queue_reason == "account_limited":
                    try:
                        await self._account_headroom_or_raise(params)
                    except AllAccountsBusy:
                        capacity_full.add(sid)
                        log.debug("session_queue.account_limited id=%s — left queued", sid)
                        continue
```

- [ ] **Step 4: Run** the new file, then `test_session_cap_enqueue.py test_product_capacity_queue.py test_queue_capacity.py test_queue_host_capacity_drain.py`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat(sessions): queue a manual create when every account is at its ceiling`.

---

### Task 3: "Start now": force one queued session past slot caps and product holds

**Files:**
- Modify: `server/sessions/orchestrator.py`: `_maybe_start_queued` gains `force: str | None = None`; new public `start_queued_now`
- Modify: `server/ipc/orchestrator_rpc.py` (add `"start_queued_now"` to the per-session method list next to `"session_queue_cancel"`)
- Modify: `server/sessions/fleet_orchestrator.py` (`_PER_SESSION` += `"start_queued_now"`)
- Modify: `server/sessions/remote.py` (method beside `session_queue_cancel`, reconstructing `AllAccountsBusy`)
- Modify: `server/sessions/facade.py` (pass-through beside `session_queue_cancel`)
- Modify: `server/api/sessions.py` (route `POST /{session_id}/start-now`)
- Test: create `server/tests/test_queue_start_now.py`; extend the API tests in `server/tests/test_session_queue_api.py`

**Interfaces:**
- Consumes: `sq.entry_for`, `_account_headroom_or_raise`, `"account_limited"` (Task 2).
- Produces: `SessionOrchestrator.start_queued_now(session_id: str) -> str`, returning one of `"started" | "not_queued" | "failed"` and raising `AllAccountsBusy` for a still-limited `account_limited` entry.
- Produces: HTTP `POST /api/sessions/{id}/start-now` → 200 `{"ok": true, "status": "started"}`; 404 `not_queued`; 409 `{"error":{"code":"all_accounts_limited"},"earliestReset":…}`; 409 `{"error":{"code":"start_failed","message":…}}`. Audit type `session.queue_start_now` with detail `{session_id, queue_reason}`.

- [ ] **Step 1: Write the failing orchestrator tests** in `server/tests/test_queue_start_now.py`

```python
"""Start now (2026-10-08): a queued session starts immediately over the global cap,
a host cap and a product cap — but never over an account ceiling."""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest

from server.sessions import session_queue as sq
from server.tests.test_product_capacity_queue import _make_capacity_orch, _seed_product_at_cap
from server.tests.test_session_cap_enqueue import _make_orch, _stop_orch
from viberpit.selector import AllAccountsBusy


def test_start_now_overrides_global_cap(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'a.db'}",
                                tmp_path=tmp_path, max_active_sessions=1)
        try:
            first = await orch.create_session("one", cwd=str(tmp_path / "reporoot"))
            queued = await orch.create_session("two", cwd=str(tmp_path / "reporoot"))
            assert first.status != "queued" and queued.status == "queued"
            assert await orch.start_queued_now(queued.id) == "started"
            assert orch.get_session(queued.id).status != "queued"
            assert await sq.entry_for(queued.id) is None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_overrides_product_cap(tmp_path):
    async def run():
        orch = await _make_capacity_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'b.db'}",
                                         tmp_path=tmp_path)
        try:
            await _seed_product_at_cap("acme/widgets")
            rec = await orch.create_session("m", repo="acme/widgets", initial_prompt="hi",
                                            cwd=str(tmp_path / "reporoot"))
            assert rec.status == "queued"
            assert await orch.start_queued_now(rec.id) == "started"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_refuses_account_limited_and_keeps_it_queued(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            def busy(*a, **k):
                raise AllAccountsBusy(datetime(2030, 1, 1), "at_ceiling")
            with patch("server.sessions.orchestrator.select_account", side_effect=busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"))
                with pytest.raises(AllAccountsBusy):
                    await orch.start_queued_now(rec.id)
            assert orch.get_session(rec.id).status == "queued"
            assert await sq.entry_for(rec.id) is not None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_unknown_or_unqueued(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            live = await orch.create_session("m", cwd=str(tmp_path / "reporoot"))
            assert await orch.start_queued_now(live.id) == "not_queued"
            assert await orch.start_queued_now("nope") == "not_queued"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_queued_now_is_per_session_routed():
    from server.sessions.fleet_orchestrator import _PER_SESSION
    assert "start_queued_now" in _PER_SESSION
```

- [ ] **Step 2: Run** them. Expected: FAIL (`start_queued_now` missing).

- [ ] **Step 3a: `force` in the drain.** Change the signature to `async def _maybe_start_queued(self, force: str | None = None) -> int:` and add `forcing_done = force is None` before the lock. Inside `while True:`, restructure the head so the forced entry is taken first and bypasses the gates:

```python
            while True:
                forcing = not forcing_done
                if not forcing:
                    if self._active_session_count() >= await self._max_active_sessions():
                        break
                    if local_cap is not None and await self.owned_active_session_count() >= local_cap:
                        log.debug(...)  # unchanged
                        break
                    entry = await sq.next_entry(exclude=foreign | capacity_full)
                else:
                    # "Start now": the named entry, over every slot gate (user override,
                    # 2026-10-08). Taken once; the loop then drains normally.
                    forcing_done = True
                    entry = await sq.entry_for(force)
                if entry is None:
                    if forcing:
                        continue
                    break
```

Then wrap the existing `product_capacity` and `runner_capacity` re-checks in `if not forcing:` so they're skipped for the forced entry. Leave the stale-entry, ownership, `account_limited` (Task 2), spawn and failure handling exactly as they are. The account ceiling is deliberately **not** overridable.

- [ ] **Step 3b: Public method** beside `maybe_start_queued`:

```python
    async def start_queued_now(self, session_id: str) -> str:
        """Start one queued session immediately, over the global cap, this host's cap
        and product/runner caps (a person's explicit override). An account ceiling is
        NOT overridden: AllAccountsBusy propagates and the session stays queued."""
        from server.sessions import session_queue as sq
        entry = await sq.entry_for(session_id)
        if entry is None:
            return "not_queued"
        if (entry.params or {}).get("queue_reason") == "account_limited":
            await self._account_headroom_or_raise(dict(entry.params))
        log.info("session_queue.start_now id=%s reason=%s", session_id,
                 (entry.params or {}).get("queue_reason", "global_cap"))
        await self._maybe_start_queued(force=session_id)
        rec = self._records.get(session_id)
        if rec is not None and rec.status not in ("queued", "ended"):
            return "started"
        return "failed" if rec is not None and rec.status == "ended" else "not_queued"
```

> If the forced entry was skipped as foreign (another host owns it and can start it), the record is still `queued` and the method returns `"not_queued"`. Per-session routing (Step 3c) makes that unreachable in the fleet. That's fine.

- [ ] **Step 3c: Wire the transport.** Add `"start_queued_now"` to `_PER_SESSION` (`fleet_orchestrator.py`) and to the RPC method list in `orchestrator_rpc.py` (next to `"session_queue_cancel"`). In `remote.py`:

```python
    async def start_queued_now(self, session_id: str) -> str:
        try:
            r = await self._client.call("start_queued_now", session_id=session_id)
        except DaemonError as exc:
            reconstructed = reconstruct_create_session_error(exc)
            if reconstructed is exc:
                raise
            raise reconstructed from exc
        return str(r)
```

(Check how `remote.py` imports `reconstruct_create_session_error`. It's used at ~line 604.) In `facade.py`: `async def start_queued_now(self, *a, **k): return await _maybe_await(self._orch.start_queued_now(*a, **k))`.

- [ ] **Step 3d: API route** in `server/api/sessions.py`, after `queue_cancel_session`, following its 404 pattern:

```python
@router.post("/{session_id}/start-now")
async def queue_start_now(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: str = Depends(current_identity),
):
    """Start a queued session now, over slot caps and product holds (never over an
    account usage ceiling — that 409s with earliestReset and the session stays queued)."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None or record.status != "queued":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )
    try:
        outcome = await orch.start_queued_now(session_id)
    except AllAccountsBusy as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={
            "error": {"code": "all_accounts_limited",
                      "message": "Every account is at its usage ceiling; this session "
                                 "starts automatically when quota frees."},
            "earliestReset": iso_utc(exc.earliest_reset)})
    await record_audit(db=db, type="session.queue_start_now", identity=identity,
                       detail={"session_id": session_id, "outcome": outcome})
    if outcome == "not_queued":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"})
    if outcome == "failed":
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={
            "error": {"code": "start_failed",
                      "message": "The session could not start and was ended; see the daemon log."}})
    return {"ok": True, "status": outcome}
```

Use the same `get_db` dependency and commit convention as `create_session`'s audit (look at how `record_audit` is committed around line 1006 and copy it).

- [ ] **Step 3e: API tests.** In `server/tests/test_session_queue_api.py`, copy the file's existing queue-cancel test fixture and add: 404 for an unqueued id; 200 `{"ok": True, "status": "started"}` when the stub orchestrator's `start_queued_now` returns `"started"`; 409 `all_accounts_limited` with `earliestReset` when it raises `AllAccountsBusy`. Assert an `AuditEvent` of type `session.queue_start_now` was written on the 200 path.

- [ ] **Step 4: Run** the new file, `test_session_queue_api.py`, and the Task 2 queue files. Then run the per-session completeness test (`grep -ln "_PER_SESSION" server/tests`) and the RPC-surface test (`grep -ln "orchestrator_rpc" server/tests`). Expected: all PASS.
- [ ] **Step 5: Commit** `feat(sessions): Start now — force a queued session past slot caps and product holds`.

---

### Task 4: Name `account_limited` in `queueStallReason`, and return the reason on a queued create

**Files:**
- Modify: `server/api/sessions.py`: `_queue_stall_reasons` / `_per_session_stall_reasons` (~576-660) and `create_session`'s queued branch (~1013-1030)
- Test: `server/tests/test_queue_stall_reason.py`

**Interfaces:**
- Consumes: the `"account_limited"` queue reason (Task 2).
- Produces: stall reason `{"code": "account_limited", "waitedSeconds": int, "earliestReset": str | None, "message": str}`; `_queue_stall_reasons(records, *, grace_s: float = QUEUE_WAIT_GRACE_S)`; the 201 body of a queued create carries `queueStallReason`.

- [ ] **Step 1: Failing tests** (append, mirroring the file's existing per-session tests and fixtures):
  - an entry whose `params={"queue_reason": "account_limited"}`, queued 120 s ago, with `server.api.sessions.select_account` patched to raise `AllAccountsBusy(datetime(2030,1,1,10), "at_ceiling")`, yields `code == "account_limited"`, `earliestReset == "2030-01-01T10:00:00Z"` (whatever `iso_utc` emits) and a message containing `"usage ceiling"`;
  - the same entry with `select_account` returning normally yields `code == "waiting_for_drain"`;
  - `_queue_stall_reasons(records, grace_s=0)` reports an entry enqueued 1 s ago (no grace).

- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Thread `grace_s` through both functions, replacing the `QUEUE_WAIT_GRACE_S` literal comparison. Add a branch beside `runner_capacity`:

```python
        elif kind == "account_limited":
            try:
                engine = parse_agent_engine(params.get("engine"), legacy_default=True)
                await asyncio.to_thread(select_account, reserve=False, engine=engine,
                                        model=quota_model(params.get("model"), engine=engine))
            except AllAccountsBusy as busy:
                reset = iso_utc(busy.earliest_reset)
                out[record.id] = {
                    "code": "account_limited", "waitedSeconds": waited, "earliestReset": reset,
                    "message": ("Every account that can run this model is at its usage "
                                "ceiling, so this session waits for quota"
                                + (f" (earliest reset {reset})" if reset else "")
                                + ". It starts automatically; Start now cannot override "
                                  "an account limit.")}
                continue
```

Import `select_account`/`quota_model`/`parse_agent_engine` at module level, from the same modules the orchestrator uses, so tests can patch `server.api.sessions.select_account`. In `create_session`'s queued branch, compute `stall = (await _queue_stall_reasons([record], grace_s=0)).get(record.id)` best-effort (wrap in `try/except Exception: log.warning(...)`) and pass `queue_stall_reason=stall` into `_record_to_dict`.
- [ ] **Step 4: Run** `test_queue_stall_reason.py` and the session-create API tests (`grep -ln "def test.*create" server/tests/test_api_sessions*.py`). Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): explain account-limited queueing and return the reason on a queued create`.

---

### Task 5: Web: show why it's queued, offer "Start now", never swallow "all accounts limited"

**Files:**
- Modify: `web/src/api/types.ts` (`Session.queueStallReason`)
- Modify: `web/src/api/hooks.ts` (`useQueueStartNow`, beside `useQueueCancel`)
- Modify: `web/src/features/sessions/list/QueuedSection.tsx`
- Modify: `web/src/shell/regions/PromptDock.tsx` (queued toast; inline all-limited)
- Modify: every test file that hand-mocks `@/api/hooks` and renders `QueuedSection` (find with `grep -rln "useQueueCancel" web/src --include=*.test.tsx`)
- Test: `web/src/features/sessions/list/QueuedSection.test.tsx` (create if absent), `web/src/shell/regions/PromptDock.test.tsx`

**Interfaces:**
- Consumes: the `POST /sessions/{id}/start-now` contract (Task 3); the `queueStallReason` shape (Task 4).
- Produces: `useQueueStartNow(): UseMutationResult<{ ok: boolean; status: string }, ApiRequestError, { id: string }>`.

- [ ] **Step 1: Types + hook.** In `types.ts`, on `Session`:

```ts
  /** Why this queued session has not started (server `_queue_stall_reasons`). */
  queueStallReason?: {
    code: string
    message: string
    waitedSeconds?: number
    earliestReset?: string | null
  } | null
```

In `hooks.ts`, after `useQueueCancel`:

```ts
export function useQueueStartNow() {
  const qc = useQueryClient()
  return useMutation<{ ok: boolean; status: string }, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.post<{ ok: boolean; status: string }>(`/sessions/${id}/start-now`, {}),
    onSettled: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}
```

- [ ] **Step 2: Failing component tests** (`QueuedSection.test.tsx`, following the mocking style of whichever existing test already renders `QueuedSection`):
  - renders `session.queueStallReason.message` under the session's name;
  - with no stall reason, shows no reason line (the section blurb stays);
  - clicking the button named `Start Alpha now, over capacity limits` calls the `useQueueStartNow` mock's `mutate` with `{ id: 's1' }`;
  - when the mutation's `onError` gets an `ApiRequestError` whose body is `{error:{code:'all_accounts_limited'}}`, a `role="status"` line reads "Account limit — starts automatically when quota frees".

- [ ] **Step 3: Implement QueuedSection.** Change the blurb to `Waiting for capacity.` (the queue now holds more than global-cap overflow), keeping the "See capacity" link. Under the name inside the open button, render `session.queueStallReason?.message` as `<div className="text-xs text-ink-muted line-clamp-2">`. In the actions group, before the cancel `X`, add a text button labelled **Start now** (`aria-label={`Start ${session.name} now, over capacity limits`}`) that calls `startNow.mutate({ id: session.id }, { onError })`. Keep a `useState<Record<string,string>>` of per-row error text: an `all_accounts_limited` code maps to the copy above, anything else to `extractMutationErrorMessage(err, "Couldn't start it")` (find it with `grep -rn "export function extractMutationErrorMessage" web/src`). Render the error as `<p role="status" className="text-xs text-danger">`. Hide **Start now** when `queueStallReason?.code === 'account_limited'`, since it can't override that and the reason line already says so.

- [ ] **Step 4: Failing PromptDock tests**:
  - a create resolving with `{status: 'queued', queueStallReason: {code: 'account_limited', message: 'M'}}` calls `toast` with a string containing `Queued` and `M`;
  - a create rejecting with a 409 `all_accounts_limited` still calls `onAllLimited` **and** rejects with a `DispatchError` whose message is non-empty (today it rejects with the raw error, and the bar shows nothing).

- [ ] **Step 5: Implement PromptDock.** In `onSuccess`, before `openSession`:

```ts
            if (session.status === 'queued') {
              toast(`Queued — ${session.queueStallReason?.message ?? 'waiting for capacity. Use Start now in the queue to override.'}`)
            }
```

In `onError`'s `all-limited` branch, replace `reject(err)` with `reject(new DispatchError(createSessionFailureMessage(failure)))` (import it from `createSessionFailure`). Update the comment above it: the banner is a bonus that `AppShell` may clear the same render whenever any account ranks, so the bar must say it itself.

- [ ] **Step 6: Add `useQueueStartNow: () => ({ mutate: vi.fn() })`** (or the file's equivalent shape) to every hand-written `@/api/hooks` mock found in Files.
- [ ] **Step 7: Run** `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`. Expected: all green.
- [ ] **Step 8: Commit** `feat(web): show why a session is queued, add Start now, never swallow all-accounts-limited`.

---

### Task 6: Full gate, docs, PR

- [ ] Add a short "Capacity override" subsection to `docs/product/features/` wherever the queue/capacity feature doc lives (`grep -rl "queueStallReason\|max_active_sessions" docs/product`): what queues, what **Start now** overrides, and what it doesn't (account ceilings).
- [ ] Run the four-step `lint-and-test` gate: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `PYTHONPATH=$PWD/src pytest tests/`, then the full `pytest server/tests/ -n auto --dist loadfile --timeout=300` in the background with `tee /tmp/capacity-override-server.log` (one full run at a time).
- [ ] Push, then `gh pr create` with a body file: `Fixes` nothing (no issue). Summarize the incident and the five Review Focus items, and call out the mixed-version degradation (Review Focus 5). Then `gh pr merge <#> --squash --auto`.
