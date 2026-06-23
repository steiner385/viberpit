# Near-real-time Deploy via Graceful Drain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (this
> project mandates subagent-driven execution) to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deploy "defer-until-idle" gate with a server-owned graceful-drain handshake so
daemon-code deploys apply within one in-flight turn, with the pending update visible in the UI and
the user's next prompt auto-queued across the restart.

**Architecture:** The session daemon gains a `DRAINING` state: it refuses to start new turns,
broadcasts an `updatePending` lifecycle event (which already flows daemon→edge→browser untouched),
and reports "ready" once no session is mid-turn. The deploy script asks the daemon to drain over its
Unix socket, waits for ready (hard-capped), then restarts. The browser buffers prompts client-side
while pending and auto-resends on reconnect. Sessions already resume from DB + transcript, so only
the single in-flight turn is ever at risk.

**Tech Stack:** Python (asyncio, FastAPI/uvicorn daemon over a Unix socket), Bash + bats for the
deploy script, React/TypeScript + Vitest for the web client.

## Global Constraints

- **Governance:** comply with `.specify/memory/constitution.md` (esp. II per-process isolation,
  IV security-first, V TDD/quality-gates, VI single-source-of-truth/no-dead-code).
- **No new DB columns** in this plan — all drain state is in-memory (resets on restart by design).
- **Backend tests** (run from this worktree): set `PYTHONPATH=<worktree>/src` and use the prebuilt
  deploy venv, e.g. `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/<file> -v`.
  Run with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1` and `-u CCSWITCH_ENABLE_XVFB` to match CI.
- **Backend lint gates (CI runs these first):** `ruff check server/` and `mypy server/` must pass.
- **Web tests** (run in `web/`, node_modules symlinked from the deploy worktree): `npx vitest run <file>`,
  the real type gate is `npx tsc -b --force` (NOT `tsc --noEmit`), and `npx eslint <files> --max-warnings 0`.
- **Event-type naming:** the wire `type` for the two new lifecycle frames is exactly `updatePending`
  and `updateCancelled` (camelCase, matching existing top-level frames like `commandResult`). Use
  these identical strings in Python (`_publish_event`) and TypeScript (`_handleInbound`).
- **New env vars (with defaults):** `CCSWITCH_DRAIN_SELFCLEAR_SEC=600` (daemon self-clear),
  `CCSWITCH_DEPLOY_DRAIN_CAP_SEC=180` (deploy-side wait cap), `CCSWITCH_SESSIOND_SOCKET=/run/ccswitch/sessiond.sock`.
- TDD: write the failing test first every task. Commit after each task. Branch off `master`
  (branch `feat/realtime-deploy-graceful-drain` already exists with the design spec committed).

## Refinements from the spec (please note)

Two implementation choices refine the approved design — both strictly reduce surface/risk:

1. **Drain is triggered over the daemon's Unix socket directly, not a new edge HTTP endpoint.**
   The socket (`/run/ccswitch/sessiond.sock`) is filesystem-permissioned and local-only, so this
   needs **no new auth** on a mutating endpoint (the edge endpoint the spec sketched would have had
   to be identity-exempt like `/healthz` yet mutating). Fewer moving parts, better security posture.
2. **The legacy busy idle-gate is retained for in-process (non-daemon) mode only.** The live host
   runs daemon/remote mode, where the drain handshake applies. In-process mode (dev/legacy) has no
   daemon to drain, so it keeps today's gate. This also means the existing
   `scripts/tests/auto-deploy-idle-gate.bats` stays valid unchanged (those tests run with no daemon
   reachable → in-process path).

---

### Task 1: Daemon drain state machine (orchestrator)

Add a `DRAINING` state to `SessionOrchestrator`: enter/exit methods, status reporting, a guard that
stops new turns while draining, the `updatePending`/`updateCancelled` broadcasts, and a self-clearing
timeout so a missed restart can't wedge the daemon.

**Files:**
- Modify: `server/sessions/orchestrator.py`
  - `__init__` (around line 125–176, where `_drivers`/`_records`/`_event_subscribers` are set)
  - `send_prompt` (lines 1062–1079)
  - Add new methods near the existing `active_session_count`/`restart_safe` block (lines 750–806)
- Test: `server/tests/test_orchestrator_drain.py` (new)

**Interfaces:**
- Consumes (existing): `self._records`, `self.active_session_count() -> int`,
  `self._publish_event(event: dict) -> None`, `self._ACTIVE_STATUSES`.
- Produces (used by Task 2 RPCs and Task 4 client):
  - `async def begin_drain(self, selfclear_sec: float | None = None) -> dict`
  - `def cancel_drain(self) -> dict`
  - `def drain_status(self) -> dict` returning `{"state": "idle"|"draining"|"ready", "busySessions": int, "elapsed": float}`
  - Lifecycle events `{"type": "updatePending"}` and `{"type": "updateCancelled"}` on the global stream.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_drain.py`:

```python
"""Graceful-drain state machine on SessionOrchestrator."""
import asyncio
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _rec(sid: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=sid, name=sid, account="acct", claude_session_id="cs",
        cols=80, rows=24, cwd="/tmp", status=status,
    )


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _drain_events(o: SessionOrchestrator) -> list[dict]:
    q = o.subscribe_events()
    events: list[dict] = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


@pytest.mark.asyncio
async def test_drain_status_idle_when_not_draining():
    o = _orch()
    assert o.drain_status()["state"] == "idle"


@pytest.mark.asyncio
async def test_begin_drain_with_no_active_sessions_is_ready():
    o = _orch()
    q = o.subscribe_events()
    st = await o.begin_drain(selfclear_sec=999)
    assert st["state"] == "ready"
    assert st["busySessions"] == 0
    assert any(e["type"] == "updatePending" for e in _drain_events_from(q))
    o.cancel_drain()


@pytest.mark.asyncio
async def test_begin_drain_with_active_session_is_draining():
    o = _orch()
    o._records["a"] = _rec("a", "in_progress")
    st = await o.begin_drain(selfclear_sec=999)
    assert st["state"] == "draining"
    assert st["busySessions"] == 1
    o.cancel_drain()


@pytest.mark.asyncio
async def test_cancel_drain_publishes_cancelled_and_returns_idle():
    o = _orch()
    q = o.subscribe_events()
    await o.begin_drain(selfclear_sec=999)
    st = o.cancel_drain()
    assert st["state"] == "idle"
    assert any(e["type"] == "updateCancelled" for e in _drain_events_from(q))


@pytest.mark.asyncio
async def test_self_clear_cancels_drain_after_timeout():
    o = _orch()
    o._records["a"] = _rec("a", "in_progress")  # stays busy so it can't go ready
    await o.begin_drain(selfclear_sec=0.05)
    await asyncio.sleep(0.15)
    assert o.drain_status()["state"] == "idle"  # self-cleared


def _drain_events_from(q) -> list[dict]:
    out: list[dict] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_orchestrator_drain.py -v`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute 'drain_status'`.

- [ ] **Step 3: Add drain state fields to `__init__`**

In `server/sessions/orchestrator.py`, inside `__init__` next to `self._ensure_lock` (line ~176), add:

```python
        # Graceful-drain state (deploy handshake). In-memory only; a restart clears it.
        self._draining: bool = False
        self._drain_started_at: float = 0.0
        self._drain_timer: asyncio.Task | None = None
```

Ensure `import time` and `import os` are present at the top of the file (add if missing).

- [ ] **Step 4: Add the drain methods**

Add near `restart_safe` (after line ~806):

```python
    def drain_status(self) -> dict:
        """Report drain state: 'idle' (not draining), 'draining' (turns still running),
        or 'ready' (draining and nothing mid-turn — safe to restart)."""
        active = self.active_session_count()
        if not self._draining:
            state = "idle"
        elif active == 0:
            state = "ready"
        else:
            state = "draining"
        elapsed = (time.monotonic() - self._drain_started_at) if self._draining else 0.0
        return {"state": state, "busySessions": active, "elapsed": round(elapsed, 1)}

    async def begin_drain(self, selfclear_sec: float | None = None) -> dict:
        """Enter DRAINING: refuse new turns, tell the UI, and arm a self-clear timer so a
        deploy that never restarts can't wedge us. Idempotent while already draining."""
        if not self._draining:
            self._draining = True
            self._drain_started_at = time.monotonic()
            self._publish_event({"type": "updatePending"})
            timeout = (
                selfclear_sec if selfclear_sec is not None
                else float(os.environ.get("CCSWITCH_DRAIN_SELFCLEAR_SEC", "600"))
            )
            self._drain_timer = asyncio.create_task(
                self._drain_selfclear(timeout), name="drain-selfclear")
        return self.drain_status()

    def cancel_drain(self) -> dict:
        """Leave DRAINING (deploy aborted / self-clear): resume normal prompt acceptance
        and tell the UI to flush any prompts it was holding."""
        if self._draining:
            self._draining = False
            self._publish_event({"type": "updateCancelled"})
            if self._drain_timer is not None and not self._drain_timer.done():
                self._drain_timer.cancel()
            self._drain_timer = None
        return self.drain_status()

    async def _drain_selfclear(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if self._draining:
            log.warning("drain self-clear: no restart within %.0fs — cancelling drain", timeout)
            self.cancel_drain()
```

- [ ] **Step 5: Guard `send_prompt` while draining**

At the very top of `async def send_prompt(self, session_id, text, attachment_ids=None):` (line 1062),
before `d = self._drivers.get(session_id)`:

```python
        if self._draining:
            # No new turns during a deploy drain. The browser buffers and resends on
            # reconnect; this guard only catches prompts already in flight when drain began.
            log.info("send_prompt rejected: drain in progress session=%s", session_id)
            self._publish_event({"type": "updatePending"})
            return
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_orchestrator_drain.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Lint**

Run: `ruff check server/sessions/orchestrator.py server/tests/test_orchestrator_drain.py && mypy server/sessions/orchestrator.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_drain.py
git commit -m "feat(daemon): graceful-drain state machine on the orchestrator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Expose drain over the IPC RPC surface

Make `begin_drain` and `drain_status` callable on the daemon's Unix socket so the deploy script (and,
in remote mode, anything local) can drive the drain.

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (the `_SIMPLE` method list, ~line 66)
- Test: `server/tests/test_ipc_drain_rpc.py` (new)

**Interfaces:**
- Consumes: `orch.begin_drain(...)`, `orch.drain_status()` from Task 1; `register_rpc`/`_make_simple`
  registration pattern; the daemon ASGI app from `server/ipc/serve.py:build_daemon`.
- Produces: RPC methods `begin_drain` and `drain_status` reachable at `POST /rpc/<method>` returning
  `{"result": {...}}`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_drain_rpc.py`:

```python
"""begin_drain / drain_status reachable over the daemon RPC surface."""
import httpx
import pytest
from server.ipc.serve import build_daemon
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_drain_rpcs_round_trip():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    app, _ = build_daemon(orchestrator=orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as client:
        r = await client.post("/rpc/drain_status", json={})
        assert r.json()["result"]["state"] == "idle"

        r = await client.post("/rpc/begin_drain", json={"selfclear_sec": 999})
        assert r.json()["result"]["state"] == "ready"  # no active sessions

        r = await client.post("/rpc/drain_status", json={})
        assert r.json()["result"]["state"] == "ready"
    orch.cancel_drain()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_ipc_drain_rpc.py -v`
Expected: FAIL — RPC `drain_status` not registered (404 / KeyError envelope).

- [ ] **Step 3: Register the two methods**

In `server/ipc/orchestrator_rpc.py`, add `"begin_drain"` and `"drain_status"` to the `_SIMPLE` list
(~line 66) that `_make_simple` iterates. The `_make_simple` wrapper already does
`await _maybe_await(getattr(orch, name)(**params))`, which handles both the async `begin_drain` and
the sync `drain_status`, and forwards `selfclear_sec` when present.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_ipc_drain_rpc.py -v`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `ruff check server/ipc/orchestrator_rpc.py server/tests/test_ipc_drain_rpc.py && mypy server/ipc/orchestrator_rpc.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/tests/test_ipc_drain_rpc.py
git commit -m "feat(ipc): expose begin_drain/drain_status RPCs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Deploy-script drain handshake

Replace the busy idle-gate with a drain handshake for daemon-mode + daemon-affecting changes; keep
the legacy gate for in-process mode and the VNC defer.

**Files:**
- Modify: `scripts/auto-deploy.sh` (the gate block, lines ~99–168)
- Test: `scripts/tests/auto-deploy-drain.bats` (new)

**Interfaces:**
- Consumes: the `begin_drain`/`drain_status` RPCs (Task 2) over `CCSWITCH_SESSIOND_SOCKET`; existing
  `daemon_changed`, `remote_active`, `vnc_verdict` computations.
- Produces: a `drain_and_wait` shell function echoing `ready`|`cap`|`unavailable`; new env seam
  `CCSWITCH_DEPLOY_DRAIN_RESULT` for tests; `CCSWITCH_DEPLOY_DRAIN_CAP_SEC` (default 180).

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/auto-deploy-drain.bats`:

```bash
#!/usr/bin/env bats
# Drain handshake in scripts/auto-deploy.sh: in daemon (remote) mode, a daemon-affecting
# change drains the daemon (CCSWITCH_DEPLOY_DRAIN_RESULT seam) before fast-forwarding.

SCRIPT="${BATS_TEST_DIRNAME}/../auto-deploy.sh"

setup() {
  TMP="$(mktemp -d)"
  SEED="$TMP/seed"; DEPLOY="$TMP/deploy"
  git init -q "$SEED"
  git -C "$SEED" checkout -q -b master
  git -C "$SEED" config user.email t@example.com
  git -C "$SEED" config user.name tester
  mkdir -p "$SEED/scripts" "$SEED/web" "$SEED/server/sessions"
  cp "$SCRIPT" "$SEED/scripts/auto-deploy.sh"
  printf 'x\n' > "$SEED/server/sessions/orchestrator.py"   # non-edge → daemon_changed=1
  printf '/.venv/\n' > "$SEED/.gitignore"
  git -C "$SEED" add -A
  git -C "$SEED" commit -qm c1
  git clone -q "$SEED" "$DEPLOY"
  git -C "$DEPLOY" config user.email t@example.com
  git -C "$DEPLOY" config user.name tester
  printf 'y\n' > "$SEED/server/sessions/orchestrator.py"
  git -C "$SEED" commit -qam c2
}

teardown() { rm -rf "$TMP"; }

@test "remote+daemon change: drain ready → fast-forwards" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_REMOTE_ACTIVE=1 \
      CCSWITCH_DEPLOY_DRAIN_RESULT=ready \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"drained"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

@test "remote+daemon change: drain cap → forces with a notice" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_REMOTE_ACTIVE=1 \
      CCSWITCH_DEPLOY_DRAIN_RESULT=cap \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"cap"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bats scripts/tests/auto-deploy-drain.bats`
Expected: FAIL — script defers/branches on busy rather than honoring `CCSWITCH_DEPLOY_DRAIN_RESULT`.

- [ ] **Step 3: Add the `drain_and_wait` function**

In `scripts/auto-deploy.sh`, after the `vnc_verdict()` function (after line ~138), add:

```bash
# Drain handshake. Asks the daemon (over its Unix socket) to stop starting new turns and
# waits until it reports `ready` (no session mid-turn) or the cap elapses. Echoes
# ready | cap | unavailable. Test seam: CCSWITCH_DEPLOY_DRAIN_RESULT forces the verdict.
DRAIN_SOCK="${CCSWITCH_SESSIOND_SOCKET:-/run/ccswitch/sessiond.sock}"
drain_and_wait() {
  if [ -n "${CCSWITCH_DEPLOY_DRAIN_RESULT:-}" ]; then echo "$CCSWITCH_DEPLOY_DRAIN_RESULT"; return 0; fi
  curl --unix-socket "$DRAIN_SOCK" -fsS -m 5 -X POST "http://daemon/rpc/begin_drain" \
       -H 'content-type: application/json' -d '{}' >/dev/null 2>&1 || { echo unavailable; return 0; }
  local cap start body; cap="${CCSWITCH_DEPLOY_DRAIN_CAP_SEC:-180}"; start="$(date +%s)"
  while :; do
    body="$(curl --unix-socket "$DRAIN_SOCK" -fsS -m 5 -X POST "http://daemon/rpc/drain_status" \
            -H 'content-type: application/json' -d '{}' 2>/dev/null || true)"
    grep -q '"state":"ready"' <<<"$body" && { echo ready; return 0; }
    [ "$(( $(date +%s) - start ))" -ge "$cap" ] && { echo cap; return 0; }
    sleep 2
  done
}
```

- [ ] **Step 4: Restructure the gate block**

Replace the gate block (lines ~140–168, the `if [ "$daemon_changed" = "1" ] || ...` through its
matching `else`/`fi` that prints "edge-only change…") with:

```bash
# Decide whether a session-disrupting restart is imminent and how to make it safe:
#  - In-process mode (no daemon): ANY restart kills sessions → legacy busy/VNC idle-gate.
#  - Daemon mode + daemon-affecting change: graceful drain (VNC still defers across ticks).
#  - Daemon mode + edge-only change: non-disruptive → skip entirely.
if [ "$remote_active" != "1" ]; then
  # Legacy idle-gate (defer across ticks, capped). Unchanged behavior for in-process hosts.
  defer_reason=""
  if [ "$(busy_verdict)" = "busy" ]; then
    defer_reason="busy"
  elif [ "$(vnc_verdict)" = "attached" ]; then
    defer_reason="vnc-attached"
  fi
  if [ -n "$defer_reason" ]; then
    MAX_DEFER="${CCSWITCH_DEPLOY_MAX_DEFER_SEC:-1800}"
    now="$(date +%s)"
    msha=""; mts=0
    if [ -f "$DEFER_FILE" ]; then read -r msha mts < "$DEFER_FILE" || true; fi
    if [ "$msha" != "$REMOTE" ]; then
      printf '%s %s\n' "$REMOTE" "$now" > "$DEFER_FILE"
      echo "[deploy] ${defer_reason} — deferring ${REMOTE:0:9} (will retry)"; exit 0
    elif [ "$(( now - mts ))" -gt "$MAX_DEFER" ]; then
      echo "[deploy] ${defer_reason} for $(( now - mts ))s (> ${MAX_DEFER}s) — forcing deploy of ${REMOTE:0:9}" >&2
    else
      echo "[deploy] ${defer_reason} — deferring ${REMOTE:0:9} ($(( now - mts ))s so far)"; exit 0
    fi
  fi
elif [ "$daemon_changed" = "1" ]; then
  # VNC watcher? Defer across ticks (don't restart under a live viewer), capped.
  if [ "$(vnc_verdict)" = "attached" ]; then
    MAX_DEFER="${CCSWITCH_DEPLOY_MAX_DEFER_SEC:-1800}"
    now="$(date +%s)"
    msha=""; mts=0
    if [ -f "$DEFER_FILE" ]; then read -r msha mts < "$DEFER_FILE" || true; fi
    if [ "$msha" != "$REMOTE" ]; then
      printf '%s %s\n' "$REMOTE" "$now" > "$DEFER_FILE"
      echo "[deploy] vnc-attached — deferring ${REMOTE:0:9} (will retry)"; exit 0
    elif [ "$(( now - mts ))" -le "$MAX_DEFER" ]; then
      echo "[deploy] vnc-attached — deferring ${REMOTE:0:9} ($(( now - mts ))s so far)"; exit 0
    else
      echo "[deploy] vnc-attached for $(( now - mts ))s (> ${MAX_DEFER}s) — forcing deploy of ${REMOTE:0:9}" >&2
    fi
  fi
  # Graceful drain: let the in-flight turn finish, then restart.
  case "$(drain_and_wait)" in
    ready)
      echo "[deploy] drained — proceeding with ${REMOTE:0:9}" ;;
    cap)
      echo "[deploy] drain cap (${CCSWITCH_DEPLOY_DRAIN_CAP_SEC:-180}s) exceeded — forcing restart of ${REMOTE:0:9}; in-flight turn will resume from transcript" >&2 ;;
    *)
      echo "[deploy] drain unavailable — proceeding with ${REMOTE:0:9} (post-restart health gate guards the rest)" >&2 ;;
  esac
else
  echo "[deploy] edge-only change, remote mode — non-disruptive restart, skipping idle gate"
fi
```

- [ ] **Step 5: Run the new and existing deploy tests**

Run: `bats scripts/tests/auto-deploy-drain.bats scripts/tests/auto-deploy-idle-gate.bats scripts/tests/auto-deploy-vnc-gate.bats scripts/tests/auto-deploy-defer-worktree.bats scripts/tests/auto-deploy-dirty-tree.bats`
Expected: all PASS (drain tests green; the legacy idle-gate/VNC tests still pass because they run with
no daemon reachable → in-process path).

- [ ] **Step 6: Lint the script**

Run: `shellcheck scripts/auto-deploy.sh` (if available) — expect no new errors.

- [ ] **Step 7: Commit**

```bash
git add scripts/auto-deploy.sh scripts/tests/auto-deploy-drain.bats
git commit -m "feat(deploy): graceful drain handshake replaces busy idle-gate (daemon mode)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Client-side prompt buffer + auto-resend (SessionSocket)

Centralize the queueing in `SessionSocket`: while an update is pending, `prompt()` buffers instead of
sending; on the post-restart reconnect (or on `updateCancelled`), buffered prompts flush automatically.

**Files:**
- Modify: `web/src/ws/client.ts`
  - inbound message types (~line 15–45), `EventMap` (~line 36–45), `WsInboundMsg` union
  - `_handleInbound` (lines 226–261), `prompt()` (lines 145–147), `connect()` on/close handlers (lines 84–143)
- Test: `web/src/ws/client.test.ts` (extend)

**Interfaces:**
- Consumes (existing): `this._emit`, `this._send`, the `MockWebSocket` test harness with
  `simulateOpen`/`simulateMessage`/`simulateClose`.
- Produces (used by Task 5): `updatePendingChange` event carrying `{active: boolean, count: number}`;
  getter `get updatePendingState(): {active: boolean; count: number}`.

- [ ] **Step 1: Write the failing test**

Append to `web/src/ws/client.test.ts`:

```typescript
describe('SessionSocket update-pending queue', () => {
  it('buffers prompts while pending and resends them on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()

    ws.simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'hello', [])
    // Not sent while pending (only the attach frame from open, no prompt frame):
    expect(ws.sent.some((s) => s.includes('"type":"prompt"'))).toBe(false)

    // Daemon restarts: socket drops then reconnects.
    ws.simulateClose()
    sock.connect()
    ws = lastSock()
    ws.simulateOpen()

    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('hello'))).toBe(true)
  })

  it('flushes immediately on updateCancelled (no restart)', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    ws.simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'queued one', [])
    ws.simulateMessage({ type: 'updateCancelled' })

    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('queued one'))).toBe(true)
  })

  it('emits updatePendingChange with active flag and count', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    const handler = vi.fn()
    sock.on('updatePendingChange', handler)
    lastSock().simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'a', [])

    expect(handler).toHaveBeenLastCalledWith({ active: true, count: 1 })
  })
})
```

(If `lastSock()` is not already a helper in this file, add `const lastSock = () => MockWebSocket.instances[MockWebSocket.instances.length - 1]`.)

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.test.ts`
Expected: FAIL — prompts are sent immediately; no `updatePendingChange` event.

- [ ] **Step 3: Add inbound types and EventMap entries**

In `web/src/ws/client.ts`, add to the inbound message interfaces:

```typescript
export interface WsUpdatePendingMsg   { type: 'updatePending' }
export interface WsUpdateCancelledMsg { type: 'updateCancelled' }
```

Add both to the `WsInboundMsg` union. Add to `EventMap` (lines 36–45):

```typescript
  updatePendingChange: (s: { active: boolean; count: number }) => void
```

- [ ] **Step 4: Add buffer state, helpers, and the getter**

Add private fields alongside the other `SessionSocket` state:

```typescript
  private _updatePending = false
  private _pendingPrompts: { sessionId: string; text: string; attachmentIds?: string[] }[] = []
  private _droppedWhilePending = false

  get updatePendingState(): { active: boolean; count: number } {
    return { active: this._updatePending, count: this._pendingPrompts.length }
  }

  private _emitUpdatePending() {
    this._emit('updatePendingChange', { active: this._updatePending, count: this._pendingPrompts.length })
  }

  private _flushPending() {
    const pending = this._pendingPrompts
    this._pendingPrompts = []
    this._updatePending = false
    this._droppedWhilePending = false
    for (const p of pending) {
      this._send({ type: 'prompt', sessionId: p.sessionId, text: p.text,
        ...(p.attachmentIds && p.attachmentIds.length ? { attachmentIds: p.attachmentIds } : {}) })
    }
    this._emitUpdatePending()
  }
```

- [ ] **Step 5: Wire `_handleInbound`, `prompt`, and the open/close handlers**

In `_handleInbound` (lines 226–261), add cases before `default`:

```typescript
    case 'updatePending':
      this._updatePending = true
      this._emitUpdatePending()
      break
    case 'updateCancelled':
      this._flushPending()  // self-clear / aborted deploy — no restart happened
      break
```

Replace `prompt()` (lines 145–147) with:

```typescript
  prompt(sessionId: string, text: string, attachmentIds?: string[]) {
    if (this._updatePending) {
      this._pendingPrompts.push({ sessionId, text, attachmentIds })
      this._emitUpdatePending()
      return
    }
    this._send({ type: 'prompt', sessionId, text, ...(attachmentIds && attachmentIds.length ? { attachmentIds } : {}) })
  }
```

In `connect()`, in `ws.onopen` (after the existing re-attach at line ~95), add:

```typescript
    if (this._droppedWhilePending) this._flushPending()
```

In `ws.onclose` (line ~136, before `_scheduleReconnect`), add:

```typescript
    if (this._updatePending) this._droppedWhilePending = true
```

- [ ] **Step 6: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/ws/client.test.ts`
Expected: PASS.

- [ ] **Step 7: Type + lint**

Run (in `web/`): `npx tsc -b --force && npx eslint src/ws/client.ts src/ws/client.test.ts --max-warnings 0`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.test.ts
git commit -m "feat(web): buffer prompts during deploy drain, resend on reconnect

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Update-pending banner + Dashboard wiring

Surface the pending state: a `useUpdatePending` hook reads the socket's state, and an
`UpdatePendingBanner` renders above the dashboard tabs (mirroring `AllLimitedBanner`).

**Files:**
- Create: `web/src/components/UpdatePendingBanner.tsx`
- Create: `web/src/hooks/useUpdatePending.ts`
- Create: `web/src/components/__tests__/UpdatePendingBanner.test.tsx`
- Modify: `web/src/components/Dashboard.tsx` (import + render near line 1620, after `AllLimitedBanner`)

**Interfaces:**
- Consumes: `getSessionSocket()` and the `updatePendingChange` event + `updatePendingState` getter from Task 4.
- Produces: `useUpdatePending(): { active: boolean; count: number }`; `<UpdatePendingBanner queuedCount={number} />`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/UpdatePendingBanner.test.tsx`:

```typescript
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UpdatePendingBanner } from '../UpdatePendingBanner'

describe('UpdatePendingBanner', () => {
  it('explains the update is applying', () => {
    render(<UpdatePendingBanner queuedCount={0} />)
    expect(screen.getByText(/update in progress/i)).toBeInTheDocument()
  })

  it('reports how many messages are queued', () => {
    render(<UpdatePendingBanner queuedCount={2} />)
    expect(screen.getByText(/2 message/i)).toBeInTheDocument()
  })

  it('omits the queue line when nothing is queued', () => {
    render(<UpdatePendingBanner queuedCount={0} />)
    expect(screen.queryByText(/will send after/i)).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/UpdatePendingBanner.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the banner**

Create `web/src/components/UpdatePendingBanner.tsx`:

```typescript
import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

interface UpdatePendingBannerProps {
  queuedCount: number
}

export function UpdatePendingBanner({ queuedCount }: UpdatePendingBannerProps) {
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-3 rounded-lg border border-sky-300 dark:border-sky-700',
        'bg-sky-50 dark:bg-sky-900/20 px-4 py-3',
      )}
    >
      <RefreshCw size={18} className="text-sky-700 dark:text-sky-400 flex-shrink-0 mt-0.5 animate-spin" aria-hidden="true" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-sky-900 dark:text-sky-200">
          Update in progress
        </p>
        <p className="text-xs text-sky-700 dark:text-sky-400 mt-0.5">
          Finishing the current turn, then applying a brief restart.
          {queuedCount > 0 ? (
            <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} will send after it completes.</>
          ) : null}
        </p>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/UpdatePendingBanner.test.tsx`
Expected: PASS.

- [ ] **Step 5: Create the hook**

Create `web/src/hooks/useUpdatePending.ts`:

```typescript
import { useEffect, useState } from 'react'
import { getSessionSocket } from '@/ws/client'

export function useUpdatePending(): { active: boolean; count: number } {
  const socket = getSessionSocket()
  const [state, setState] = useState(socket.updatePendingState)
  useEffect(() => {
    const handler = (s: { active: boolean; count: number }) => setState(s)
    socket.on('updatePendingChange', handler)
    return () => socket.off('updatePendingChange', handler)
  }, [socket])
  return state
}
```

- [ ] **Step 6: Render in Dashboard**

In `web/src/components/Dashboard.tsx`, add the import near the other component imports:

```typescript
import { UpdatePendingBanner } from './UpdatePendingBanner'
import { useUpdatePending } from '@/hooks/useUpdatePending'
```

Inside the `Dashboard` component body (near the `allLimitedReset` state, ~line 1273), add:

```typescript
  const updatePending = useUpdatePending()
```

In the returned JSX, immediately after the `AllLimitedBanner` block (~line 1625), add:

```typescript
        {updatePending.active && (
          <UpdatePendingBanner queuedCount={updatePending.count} />
        )}
```

- [ ] **Step 7: Type, lint, and full web test run**

Run (in `web/`): `npx tsc -b --force && npx eslint src/components/UpdatePendingBanner.tsx src/hooks/useUpdatePending.ts src/components/Dashboard.tsx --max-warnings 0 && npx vitest run src/components/__tests__/UpdatePendingBanner.test.tsx src/ws/client.test.ts`
Expected: clean + PASS.

- [ ] **Step 8: Commit**

```bash
git add web/src/components/UpdatePendingBanner.tsx web/src/hooks/useUpdatePending.ts web/src/components/__tests__/UpdatePendingBanner.test.tsx web/src/components/Dashboard.tsx
git commit -m "feat(web): update-pending banner wired to the drain handshake

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before PR)

- [ ] Backend: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest server/tests/test_orchestrator_drain.py server/tests/test_ipc_drain_rpc.py -v` (and a broad `server/tests` run to catch regressions), then `ruff check server/ && mypy server/`.
- [ ] Deploy: `bats scripts/tests/` (all five files green).
- [ ] Web: `npx tsc -b --force && npx eslint src --max-warnings 0 && npx vitest run`.
- [ ] Open a non-draft PR with `Fixes` referencing the starvation issue if one is tracked; arm `gh pr merge --squash --auto`.

## Spec coverage check

- Server-owned drain handshake → Tasks 1–2 (state machine + RPC), Task 3 (script driver).
- Drain boundary = current turn only; new prompts queued → Task 1 `send_prompt` guard + Task 4 client buffer.
- Hard cap → Task 3 `CCSWITCH_DEPLOY_DRAIN_CAP_SEC` (default 180).
- Self-clearing timeout → Task 1 `_drain_selfclear` (`CCSWITCH_DRAIN_SELFCLEAR_SEC`, default 600).
- Visible "update pending" + queued count → Tasks 4–5 (event + banner).
- Auto-resend on restart → Task 4 reconnect flush.
- VNC gate retained; edge-only fast path retained → Task 3 restructured conditional.
- Client-side queue (v1 trade-off) → Task 4 (in `SessionSocket`, lost on tab close — documented).
