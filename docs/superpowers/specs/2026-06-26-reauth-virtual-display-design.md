# Reauth on the Virtual X Display — Design

**Date:** 2026-06-26
**Status:** Approved design, ready for implementation plan
**Feature:** Redirect the Claude account reauth/login browser from the host's physical
`:0` display onto ccswitch's per-session virtual X implementation (Xvfb + x11vnc), and
surface it through the existing web VNC viewer so reauth can be completed remotely from
a browser (including mobile).

## Goal & Motivation

Today the reauth browser launches on `DISPLAY=:0` — the operator's *physical* console
(`server/reauth.py`, via `CCSWITCH_REAUTH_DISPLAY`, default `:0`). On a headless host, or
when the operator is on mobile, the OAuth click-through is impossible: there is no monitor
to look at. ccswitch already runs a per-session virtual X stack (`server/sessions/x11manager.py`:
Xvfb displays `:100–:111`, each exposed on demand via `x11vnc`) surfaced through a
browser-based noVNC viewer (`/ws/vnc` → `BrowserTab.tsx`).

**This feature runs the reauth browser on a virtual Xvfb display and streams it to the web
VNC viewer**, so the human signs in to Claude inside the ccswitch web UI from anywhere,
matching the project's "web-hosted, mobile-accessible" charter.

**Primary target: production, which runs in daemon/remote mode** (an edge process serves the
web app; a separate daemon owns the claude sessions and the X displays).

## Constraints & Key Facts (verified in code)

- **The browser launch is inseparable from `capture_login`.** Reauth runs
  `claude auth login --claudeai` (`src/ccswitch/auth_login.py`); *that* subprocess opens
  Chrome via the `BROWSER` launcher (`scripts/ccswitch-reauth-browser`). So "where the
  browser renders" = "where `capture_login` runs", and that host needs the account config
  dirs, the `claude` binary, **and** the Xvfb display together.
- **In remote mode those all live on the daemon, not the edge.** `app.state.x11` is built
  only off the daemon (`server/app.py:332` sets it `None` on the edge; `server/ipc/serve.py:59`
  builds it on the daemon). Account config dirs / DB are on the daemon host.
- **The VNC pipeline is keyed by an opaque string end-to-end** — frontend `?session=`,
  `/ws/vnc` proxy (`server/bridge/vnc_ws.py`), daemon `/vnc` (`server/ipc/x11_rpc.py`), and
  `X11Manager._specs[key]`. Any reserved key rides it unchanged.
- **The daemon does NOT construct a `ReauthManager` today.** `create_daemon_app()`
  (`server/ipc/daemon.py`) registers no reauth state; reauth currently executes on the edge.
- **Reauth is strictly one-at-a-time.** `ReauthManager` serializes via a lock; add-account and
  reauth share the single manager ("a single reauth Chrome/DISPLAY on the host",
  `server/api/accounts.py:250`). One reserved display key suffices.
- **`config.py` defines settings twice** (pydantic ~line 30 + a dataclass fallback ~line 98);
  any new setting must be added in both.

## Chosen Approach (Approach 1): proxy the whole reauth flow to the daemon

Run the *entire* reauth flow on the host that owns the displays (the daemon in remote mode,
the edge in-process), on a daemon-allocated Xvfb display under a reserved pseudo-session key
`__reauth__`. The edge proxies `start`/`status`/`cancel` over the existing `/rpc` channel.
The web VNC viewer rides the existing `/ws/vnc?session=__reauth__` path. This mirrors the
codebase's established `InProcessOrchestratorAdapter` vs `RemoteOrchestrator` split
(`server/app.py:318,327`).

Rejected alternatives:
- **Edge allocates a daemon display and launches Chrome itself** — only works same-host with a
  shared filesystem, reaches across the daemon's ownership boundary into its X socket/xauthority,
  breaks the unix-or-mTLS abstraction. Fragile.
- **A dedicated always-on reauth Xvfb+x11vnc with a new `?display=` VNC param** — a parallel
  re-implementation of `X11Manager` (violates constitution VI), needs its own auth whitelist,
  and still doesn't resolve who launches the browser across the edge/daemon split.

```
        EDGE (web-facing)                         DAEMON (owns claude + config + Xvfb)
   ┌────────────────────────┐               ┌──────────────────────────────────────┐
   │ /api/accounts/.../reauth│  RPC start/  │ app.state.reauth (ReauthManager)       │
   │   → RemoteReauthManager ├──status/─────►   runner:                              │
   │      (proxy)            │  cancel       │     x11.start_display("__reauth__")    │
   │                        │               │     capture_login(DISPLAY=:N,XAUTH=…)  │
   │ /ws/vnc?session=        │   /vnc?       │     finally x11.stop_display(…)        │
   │   __reauth__           ├──session=─────►   /vnc relay ← x11vnc on :N            │
   └────────────────────────┘  __reauth__   └──────────────────────────────────────┘
```

## Component 1 — Runner display lifecycle & fallback ladder

`ReauthManager.__init__` gains an injected accessor `x11: Callable[[], X11Manager | None] | None`
(same DI style as the existing `runner`/`key_available` params). A module-level constant
`REAUTH_DISPLAY_KEY = "__reauth__"` (the `__` prefix cannot collide with a UUID session id).

The production runner brackets `capture_login` with a display lease:

```python
x11 = manager._x11() if manager._x11 else None
spec = None
if x11 is not None and settings.reauth_virtual_display:
    spec = await x11.start_display(REAUTH_DISPLAY_KEY)     # leases :N + Xvfb; idempotent on key
if spec is not None:
    env_extra["DISPLAY"] = f":{spec.lease.display}"
    env_extra["XAUTHORITY"] = spec.xauthority              # NEW; chrome inherits via capture_login env
    manager._state.vnc_session = REAUTH_DISPLAY_KEY        # surfaces to the UI (Component 4)
else:
    env_extra["DISPLAY"] = settings.reauth_display          # unchanged :0 fallback; no XAUTHORITY
try:
    ... existing capture_login + mark_reauthenticated ...
finally:
    manager._state._temp_profile = None
    auth_login.cleanup_reauth_profile(temp)
    if spec is not None:
        await x11.stop_display(REAUTH_DISPLAY_KEY)           # kills Xvfb/x11vnc/chrome, unlinks, releases
```

**Fallback ladder** (never worse than today): virtual display → `:0`. We land on `:0` when xvfb
is disabled (`build_x11_manager` → `None`), binaries are missing (same), the pool is exhausted
(`start_display` → `None`), or `reauth_virtual_display=False`. `XAUTHORITY` is set only on the
virtual path, so the fallback env is byte-for-byte today's.

**Why `finally` covers all exits:** `cancel()` cancels the task → `CancelledError` propagates
through the `try/finally`; `timeout` makes `asyncio.wait_for` cancel the runner → same path;
success/error fall through. One `stop_display` in `finally` reclaims the lease on every terminal
state — no extra teardown wiring in `_run`/`cancel`.

Notes:
- The reauth display leases from the **same 12-slot session pool** (deliberate single source of
  truth; reauth is rare/transient/one-at-a-time; pool exhaustion → `:0`).
- `start_display` is idempotent on the key; combined with the one-at-a-time lock it can never
  double-allocate.
- `x11vnc` is still spawned **lazily** on first VNC attach (`ensure_vnc`), so a reauth where
  nobody opens the viewer never starts a VNC server.

## Component 2 — `ReauthState` gains `vnc_session`

```python
# server/reauth.py — ReauthState dataclass
vnc_session: str | None = None     # reserved key when a virtual display is live; None on :0 fallback
# to_dict adds:  "vncSession": self.vnc_session
```

`start()` resets state (so `vnc_session` begins `None`); the runner sets it once `start_display`
succeeds. The ~ms gap before allocation is absorbed by the 1500ms status poll. The field flows to
the edge/frontend purely through `to_dict` — no proxy-specific plumbing.

## Component 3 — Remote proxy & daemon RPC surface

A **reauth facade** with three async methods — `start(name, identity)`, `status(name)`,
`cancel(name)` — with two implementations, mirroring `InProcessOrchestratorAdapter` /
`RemoteOrchestrator`.

**Edge wiring** (`server/app.py`, replacing the unconditional construction at `:340-343`):

```python
if getattr(app.state, "remote_mode", False):
    from server.reauth_remote import RemoteReauthManager
    app.state.reauth = RemoteReauthManager(DaemonClient(endpoint))
else:
    mgr = reauth_manager or ReauthManager(
        timeout_s=get_settings().reauth_timeout_s,
        x11=lambda: app.state.x11,
    )
    app.state.reauth = InProcessReauthAdapter(mgr)     # async wrappers; preserves the test-injection param
```

**Daemon side** (`server/ipc/serve.py:build_daemon`, added to the existing registration block):

```python
from server.reauth import ReauthManager
from server.ipc.reauth_rpc import register_reauth
app.state.reauth = ReauthManager(
    timeout_s=get_settings().reauth_timeout_s,
    x11=lambda: app.state.x11,          # daemon owns x11 → virtual path is live here
)
register_reauth(app, app.state.reauth)
```

**New `server/ipc/reauth_rpc.py`** — three thin handlers (shape matches `orchestrator_rpc.py`):

```python
def register_reauth(app, reauth):
    async def reauth_start(p):  return (await reauth.start(p["name"], p["identity"])).to_dict()
    async def reauth_status(p): return reauth.status(p.get("name", "")).to_dict()
    async def reauth_cancel(p): return (await reauth.cancel(p.get("name", ""))).to_dict()
    register_rpc(app, "reauth_start", reauth_start)
    register_rpc(app, "reauth_status", reauth_status)
    register_rpc(app, "reauth_cancel", reauth_cancel)
```

`ReauthBusy`/`ReauthNoKey` surface through the existing `/rpc` envelope as
`{"error":{"type":...}}`; `RemoteReauthManager` re-raises them **by type** so the edge's
existing 409/400 mapping in `accounts.py` is unchanged.

**New `server/reauth_remote.py`** — `RemoteReauthManager` (takes a `DaemonClient`):
`start`/`status`/`cancel` call `client.call("reauth_*", ...)` and rebuild a `ReauthState` from the
returned dict; the typed-error remap raises `ReauthBusy`/`ReauthNoKey` from `DaemonError.type`.

**`InProcessReauthAdapter`** — async wrappers around a real `ReauthManager` (its `start`/`cancel`
are already async; `status` is sync → wrapped to be awaitable). Wrapping the injectable
`reauth_manager` keeps the existing test-injection contract working.

**API endpoints** (`server/api/accounts.py`): the two call sites that use `reauth.status(...)`
synchronously (`:329,:341`) become `await reauth.status(...)`; `start`/`cancel` are already
awaited. 202/409/400 behaviour unchanged.

**Daemon lifespan** (`server/ipc/serve.py:daemon_lifespan`): add
`app.state.reauth.sweep_stale()` at startup and `await app.state.reauth.shutdown()` in the
`finally` — the two calls the edge already makes (`server/app.py:167,225`).

This is **poll-based**, not streaming: reauth state already polls, so the proxy needs only unary
`/rpc`. The only streaming is VNC pixels, on the untouched `/ws/vnc → /vnc` relay. No daemon RPC
is needed for VNC: `x11_rpc.py`'s `/vnc?session=` already resolves any key in `_specs`, which the
runner populated via `start_display("__reauth__")`.

## Component 4 — Frontend: in-page reauth browser

**Web type** (`web/src/api/types.ts`): `ReauthState` gains `vncSession?: string | null`.

**New `web/src/components/ReauthBrowserPanel.tsx`** — thin wrapper around the confirmed
standalone-safe `BrowserTab`:

```tsx
export function ReauthBrowserPanel({ vncSession }: { vncSession: string }) {
  return (
    <div className="mt-2 h-[60vh] min-h-0 rounded-md border overflow-hidden">
      <BrowserTab sessionId={vncSession} statusState="working" />
    </div>
  )
}
```

**Render gate + copy fix** in both reauth surfaces (they share the one-at-a-time manager):
`web/src/components/AccountsManager.tsx` (inline `AccountRow`, the status line at ~`:428-443`) and
`web/src/components/AddAccountDialog.tsx`:

```tsx
{isReauthRunning && reauthStatus?.vncSession
  ? <>
      <p className="text-xs text-amber-600">Sign in to Claude in the window below…</p>
      <ReauthBrowserPanel vncSession={reauthStatus.vncSession} />
    </>
  : isReauthRunning && <p className="text-xs text-amber-600">Opening Chrome on the host screen — sign in there…</p>}
```

`vncSession`-gated rendering gives automatic graceful degradation: on a host with xvfb off /
pool exhausted, `vncSession` is null, the panel never mounts, and the user sees today's
"host screen" copy.

## Component 5 — Config, security, teardown, testing

**Config** (`server/config.py`, in **both** the pydantic and dataclass definitions):
- `reauth_virtual_display: bool = True` (alias `CCSWITCH_REAUTH_VIRTUAL_DISPLAY`) — master toggle;
  `False` ⇒ byte-for-byte today's `:0` behaviour.
- Existing `reauth_display` (`:0`) is retained as the fallback target.

**Security:**
- `/ws/vnc` is already identity-gated (`require_ws_identity`); `__reauth__` is just another key →
  no new auth surface. `display_for("__reauth__")` returns a spec only while a reauth is live, so
  the key is un-connectable otherwise (close `4404`).
- The `__`-prefixed reserved key cannot collide with a UUID session id (no cross-hijack).
- No change to the daemon trust boundary (socket perms / mTLS allowlist); the new RPCs are as
  trusted as `create_session`.

**Teardown matrix:**
- success / error / timeout / cancel → runner `finally` → `stop_display` releases lease + kills
  Xvfb/x11vnc/Chrome + unlinks lock/socket/xauth; an attached VNC client sees its relay close.
- daemon SIGKILL mid-reauth → leftover Xvfb/x11vnc reaped on next boot by the existing
  `sweep_orphan_processes` (in-range display, no live session) + `sweep_stale` (lock/socket files);
  reauth-tmp profile reaped by existing `ReauthManager.sweep_stale()` (600s).
- pool exhausted → `:0` fallback, `vncSession` null, legacy UI copy.

**Testing:**
- *Backend unit*: runner allocates `__reauth__` + sets `DISPLAY`/`XAUTHORITY` when x11 present;
  `:0` + no `XAUTHORITY` when x11 `None`; `stop_display` called on each of success/error/timeout/
  cancel (fake x11 spy); `to_dict` includes `vncSession`.
- *RPC*: `reauth_start/status/cancel` round-trip through `/rpc`; `ReauthBusy`/`ReauthNoKey` surface
  as typed envelope errors; `RemoteReauthManager` re-raises them.
- *Edge*: `RemoteReauthManager` ↔ daemon via fake `DaemonClient`; API 202/409/400 unchanged;
  `await reauth.status(...)` adaptation; `InProcessReauthAdapter` preserves injected fakes.
- *Frontend (full vitest, per CI)*: panel mounts iff `vncSession` truthy; copy switches; add-account
  dialog shows it too; update the existing "host machine's screen" assertion in `AccountReauth.test.tsx`.
- *Lint gates*: ruff + mypy (backend); `tsc -b` (real gate, not `tsc --noEmit`) + eslint
  `--max-warnings 0` (web).

## Out of Scope / YAGNI

- Per-account reauth displays (reauth is one-at-a-time → one reserved key).
- A generic `?display=N` VNC route (the reserved-key approach reuses the session-keyed path).
- Streaming reauth state over the event bus (polling already suffices).
- Cross-host (mTLS) reauth where config dirs are NOT shared — the daemon owns config + displays
  on one host; multi-host config sync is a separate concern.

## Implementation Order (phased, one coherent design)

1. **Backend runner + state**: `x11` accessor on `ReauthManager`, `REAUTH_DISPLAY_KEY`, lease
   bracket, `vnc_session` field + `to_dict`, `reauth_virtual_display` config (both defs). Unit tests.
   (Works end-to-end in **in-process** mode immediately.)
2. **Remote proxy**: `RemoteReauthManager`, `InProcessReauthAdapter`, `reauth_rpc.py`, daemon
   construction + lifespan sweep/shutdown, edge wiring, `await status`. RPC + edge tests.
   (Brings **daemon/remote (prod)** mode online.)
3. **Frontend**: `vncSession` type, `ReauthBrowserPanel`, render gate + copy in `AccountsManager`
   and `AddAccountDialog`. Full vitest + tsc -b + eslint.
