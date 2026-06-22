# 002 — Per-Session Virtual Browser with Human Takeover

**Status:** Draft (design approved 2026-06-22)
**Feature dir:** `specs/002-session-x-display/`
**Constitution:** complies with `.specify/memory/constitution.md` v1.1.0 — see "Constitutional notes" below.

## Summary

Give each managed `claude` session its own virtual X display so that:

1. The session's agent can drive a **real, headed browser** (e.g. Playwright with
   `headless:false`) on the server, and
2. A human can **watch and take over** that same browser remotely from a phone or
   laptop, through the existing Tailscale-authenticated web app.

The agent's browser and the human's eyes share **one framebuffer per session** — the
human sees exactly what the agent is doing and can grab the mouse/keyboard of the same
session.

## Motivation

The server hosts X-capable tooling (Playwright, etc.) but no way to use it interactively
from a remote browser. The existing `/ws` event protocol streams normalized session
events, not pixels. We want a per-session, pixel-level surface that both the agent and a
remote human can operate, gated by the same Tailscale identity that already protects the
app.

## Goals

- Per-session virtual X display (`DISPLAY=:N`) injected into the managed `claude` process
  environment at spawn.
- Agent headed-browser usage "just works" — no special tool hooks; rendering into `:N`
  is automatic once `DISPLAY` is set.
- Human takeover: an interactive noVNC canvas in the session view, lazily started on
  attach, torn down on detach/idle.
- All access flows through the existing authenticated FastAPI app — no new exposed ports.
- Off by default; opt-in per deployment.

## Non-goals (v1)

- A full remote **desktop** / window manager — v1 is browser-content only (the agent's
  Chromium is the content; no WM, no taskbar).
- Multi-user-per-session arbitration (multiple devices may attach to the same `:N` and
  share one cursor; no locking/handoff UI).
- Recording, trace export, or artifact streaming (that's the separate headless path).
- GPU acceleration / hardware video; software framebuffer only.
- A `websockify` sidecar (explicitly replaced — see Architecture).

## Concurrency & lifecycle model (decided)

- **Scope:** one virtual browser **per ccswitch session** (keyed by the session UUID).
- **Lifecycle:** **lazy start, idle teardown**, split across components:
  - **Xvfb** starts at **session create** (when the feature is enabled) so the injected
    `DISPLAY=:N` is valid the moment the agent might use it. Cost: ~30–40 MB per *live*
    session even if the browser is never touched. This is the accepted reconciliation of
    "lazy start" with the fact that process env is frozen at spawn (see Rationale).
  - **The browser** (Chromium) is **not** pre-launched — it appears when the agent's
    Playwright (or a future human "launch browser" action) starts it. Naturally on-demand.
  - **x11vnc** starts **lazily on first human attach** to `/ws/vnc` and is **torn down on
    detach or idle timeout**. Xvfb and any running agent browser persist across detach.
  - **Session end** kills x11vnc + browser + Xvfb and releases the display number.

### Rationale: why Xvfb at create, not fully lazy

`real_sdk_factory` (`server/sessions/sdk_factory.py:8-21`) builds the subprocess env once
and hands it to the SDK at spawn; `DISPLAY` cannot be injected into a running `claude`
process afterward. For the agent to use a headed browser, `DISPLAY=:N` must be set at
spawn, which requires `:N` to exist at spawn. Fully-lazy Xvfb would require detecting
"the agent is about to launch a browser," which is fragile. Starting the lightweight Xvfb
at create — while keeping the heavy x11vnc + browser on-demand — is the pragmatic
reconciliation. The whole feature is off by default, so disabled deployments pay nothing.

## Architecture

Per live session, when the feature is enabled:

```
Xvfb :N (localhost software framebuffer)        ← backs the injected DISPLAY=:N
  ├─ agent's Playwright Chromium (headless:false)   ← launched by the agent on demand
  └─ x11vnc -rfbport P -localhost -nopw             ← scrapes :N; lazy on first human attach
        ⇅ RFB over TCP (127.0.0.1:P)
   FastAPI  /ws/vnc?session=<id>                     ← byte relay; gated by require_ws_identity
        ⇅ RFB over WebSocket
   noVNC <canvas> in the session view "Browser" tab
```

**No `websockify`, no window manager.** noVNC speaks RFB-over-WebSocket; x11vnc speaks
RFB-over-TCP on localhost. A FastAPI `/ws/vnc` route relays bytes between the two, reusing
the existing `require_ws_identity` auth — removing a dependency (Principle VII) and
keeping Tailscale identity as the sole gate (Principle IV).

## Components

### New modules

- **`server/sessions/x11manager.py`**
  - `DisplayAllocator` — asyncio-locked pool of display numbers (`alloc()`, `release()`,
    raises on exhaustion). Configurable range.
  - `X11Manager` — spawn/supervise Xvfb and x11vnc per session via
    `asyncio.create_subprocess_exec`; terminate-then-kill with timeout, mirroring the
    process-shutdown pattern in `server/reauth.py:217-225`. Tracks per-session processes
    and the allocated rfbport.
- **`server/bridge/vnc_ws.py`**
  - `/ws/vnc?session=<id>` route. Auth via `require_ws_identity` (close 1008 on failure,
    matching the existing WS pattern). Looks up the session's rfbport, lazily ensures
    x11vnc is running, then bidirectionally pumps bytes between the WebSocket and the
    localhost TCP socket until either side closes. Stops x11vnc on last-detach / idle.

### Touched files

- `server/sessions/sdk_factory.py` — inject `DISPLAY=:N` into the env dict (only when the
  feature is enabled and a display is allocated for the session).
- `server/sessions/orchestrator.py`
  - `create_session` (~line 184): allocate a display, start Xvfb, register in new dicts
    mirroring the `_worktrees` registry pattern, thread the display number to the factory.
  - `close_session` (~line 285): pop and tear down display/x11vnc/browser, release the
    number — mirroring the worktree-cleanup block (`asyncio.to_thread` + `log.exception`).
  - Optional: publish `displayAllocated` / `displayFreed` events on the existing bus.
- `server/config.py` — new settings (Pydantic + dataclass fallback):
  - `CCSWITCH_ENABLE_XVFB` (bool, **default false**)
  - `CCSWITCH_DISPLAY_START` (int, default 100)
  - `CCSWITCH_DISPLAY_COUNT` (int, default 100)
  - `CCSWITCH_XVFB_RESOLUTION` (str, default `1280x1024x24`)
  - `CCSWITCH_XVFB_IDLE_TIMEOUT_S` (float, default 3600) — x11vnc idle teardown.
- `server/app.py` lifespan — on shutdown, kill all X11 processes before/within
  `orchestrator.stop()`.

### Frontend

- A **"Browser" tab** in the session view that lazily mounts a noVNC `RFB` instance
  pointed at `/ws/vnc?session=<id>`, reusing the app's authenticated WS path. Renders
  connecting / empty (no display) / error states. noVNC vendored into `web/`.

## Data flow

1. Session create → `DisplayAllocator.alloc()` → `X11Manager.start_xvfb(:N)` →
   `DISPLAY=:N` injected at spawn.
2. Agent runs headed Playwright → Chromium renders into `:N` (no extra hook).
3. Human opens Browser tab → `/ws/vnc` → lazy `X11Manager.ensure_x11vnc(session)` →
   RFB bytes relayed WS⇄TCP → noVNC paints; human input flows back.
4. Detach/idle → stop x11vnc (Xvfb + browser persist).
5. Session end → kill all, `DisplayAllocator.release(N)`.

## Error handling

- **Display pool exhausted:** `alloc()` raises; session create proceeds **without** a
  display (feature degrades gracefully — agent gets no headed browser, log a warning).
  Decision: never fail session creation because the X pool is full.
- **Xvfb/x11vnc fails to spawn or binary missing:** log `log.exception`, mark the session
  as "no display"; `/ws/vnc` returns a clean error frame; the session itself is unaffected.
- **`/ws/vnc` for a session with no display:** close with an explanatory error (not 1008),
  frontend shows the empty state.
- **Process leak prevention:** all spawns registered per-session and unconditionally torn
  down in `close_session` and app shutdown, mirroring existing worktree/PTY cleanup.
- **Idle teardown race:** x11vnc teardown guarded so a re-attach during teardown restarts
  cleanly.

## Security (Principle IV)

- x11vnc bound `-localhost -nopw`: the only reachable path is the authenticated FastAPI
  `/ws/vnc` route — Tailscale identity is the sole gate, identical to `/ws`. No VNC port
  is ever exposed beyond loopback.
- Per-frame / per-connection identity verification reuses `require_ws_identity`.
- Feature **off by default**. Interactive input means real control of a possibly
  logged-in browser, so enabling is a deliberate per-deployment choice documented in the
  quickstart.
- Display numbers and rfbports are server-allocated; clients reference only the session
  UUID they are already authorized for.

## Testing (Principle V — TDD)

- **Unit**
  - `DisplayAllocator`: alloc/release round-trip, exhaustion raises, no double-alloc.
  - `X11Manager`: spawn/teardown with a fake subprocess spawner; terminate-then-kill on
    timeout; per-session registry correctness.
  - `/ws/vnc` relay: against a fake TCP echo server — bytes pumped both directions, clean
    close on either side, auth rejection path.
  - Orchestrator: display allocated on create, released on close; pool-exhaustion
    degrades without failing create; injection only when enabled.
- **Integration** (gated on `Xvfb`/`x11vnc` present on host/CI)
  - Real Xvfb + x11vnc smoke; RFB handshake bytes relay end-to-end through `/ws/vnc`.
- Run server tests from a worktree with `PYTHONPATH=$(pwd)/src` and the prebuilt main
  `.venv` per project conventions; pin `CCSWITCH_TAILNET_ALLOWLIST` or use `CCSWITCH_DEV=1`.

## Constitutional notes

- **I (no fork):** no changes to the Claude binary; we set `DISPLAY` in its environment.
- **II (isolation):** the display number is a per-session resource allocated and reclaimed
  exactly like `CLAUDE_CONFIG_DIR`, the PTY, and worktrees.
- **III (verify real):** integration tests exercise real Xvfb/x11vnc and a real RFB
  handshake, not mocks-only.
- **IV (security-first):** loopback-only VNC, Tailscale-sole-gate, off by default.
- **VI (single source of truth):** one `X11Manager`/`DisplayAllocator`; no parallel
  display bookkeeping.
- **VII (modern deps):** no `websockify`; Xvfb/x11vnc are OS packages in the deploy layer,
  noVNC vendored in `web/` — Python dependency graph untouched.

## Phasing

1. `DisplayAllocator` + `X11Manager` (Xvfb only) + config flag + orchestrator
   alloc/inject/cleanup → agent gets a working headed browser. Fully testable headless.
2. `/ws/vnc` relay + lazy x11vnc → RFB bytes flow; relay tested against fake + real
   x11vnc.
3. noVNC "Browser" tab in the frontend → human takeover.

## Open questions

None blocking. Deferred to implementation detail: exact rfbport allocation scheme
(derive from display number vs. separate pool); whether to surface a "launch browser"
button for human-initiated (agent-independent) browsing in a later phase.
