# 002 — Per-Session Virtual Browser with Human Takeover

**Status:** Draft v2 (design approved 2026-06-22; revised after architecture / infrastructure / UX review)
**Feature dir:** `specs/002-session-x-display/`
**Constitution:** complies with `.specify/memory/constitution.md` v1.1.0 — see "Constitutional notes".

## Summary

Give each managed `claude` session its own virtual X display so that:

1. The session's agent can drive a **real, headed browser** (Playwright with
   `headless:false`) on the server, and
2. A human can **watch and take over** that same browser remotely from a phone or
   laptop, through the existing Tailscale-authenticated web app.

The agent's browser and the human's view share **one framebuffer per session** — the
human sees exactly what the agent is doing and can grab the mouse/keyboard.

**v1 use-case framing (decided):** the primary case is **watch / take over the agent's
browser**, plus a **minimal human "open tab" action** so a human opening the tab on an
idle session can make the agent's Chromium open a tab/URL and have something to see. This
is *not* a full human-owned browser — it is a thin command into the agent's existing
Playwright browser.

## Motivation

The server hosts X-capable tooling (Playwright) but no way to use it interactively from a
remote browser. The existing `/ws` event protocol streams normalized session events, not
pixels. We want a per-session, pixel-level surface that both the agent and a remote human
can operate, gated by the same Tailscale identity that already protects the app.

## Goals

- Per-session virtual X display (`DISPLAY=:N`) injected into the managed `claude` process
  environment at spawn (and preserved across resume/rotate).
- Agent headed-browser usage "just works" once `DISPLAY` is set — no per-tool hooks.
- Human takeover: an interactive noVNC canvas in the session view, lazily started on
  attach, torn down on detach/idle, with clear state feedback and a shared-control signal.
- Minimal human "open tab" action into the agent's browser.
- All access flows through the existing authenticated FastAPI app — no new exposed ports.
- Off by default; opt-in per deployment; fail-loud preflight when enabled.
- Bounded host resource use; survives the auto-deploy restart cycle without leaking.

## Non-goals (v1)

- A full remote **desktop** / window manager — v1 is browser-content only.
- A full **human-owned** browser independent of the agent (only the thin open-tab action).
- Multi-user-per-session **arbitration/locking** — multiple devices may attach to the
  same `:N` and share one cursor; v1 provides a *signal* (who's driving) but no lock.
- Recording, trace export, artifact streaming (separate headless path).
- GPU acceleration / hardware video; software framebuffer only.
- A `websockify` sidecar (explicitly replaced — see Architecture).

## Concurrency, lifecycle & resource model (decided)

- **Scope:** one virtual browser **per ccswitch session**, keyed by the session UUID.
- **Display pool (decided — small):** default `CCSWITCH_DISPLAY_COUNT=12` (not 100), sized
  for the ~19 GB host. A display is cheap; a browser is not — so a **separate concurrent
  running-browser cap** (`CCSWITCH_MAX_BROWSERS`, default 6) provides backpressure
  independent of the display pool.
- **Lifecycle — lazy start, idle teardown, split across components:**
  - **Xvfb** starts at **session create** (when enabled) so the injected `DISPLAY=:N` is
    valid the moment the agent might use it. `start_xvfb` **polls for X readiness**
    (`/tmp/.X11-unix/X{N}` or `xdpyinfo`, ~2 s / 50 ms) before returning; failure routes
    to graceful "no display" degradation. Cost: ~30–50 MB per *live* session.
  - **The browser** (Chromium) is **not** pre-launched — it appears when the agent's
    Playwright (or the human open-tab action) starts it. The running-browser cap applies
    here.
  - **x11vnc** starts **lazily on first human attach** to `/ws/vnc`, torn down on
    last-detach or idle timeout (`CCSWITCH_VNC_IDLE_TIMEOUT_S`). Xvfb + any agent browser
    persist across detach.
  - **Session end** (every exit path) kills x11vnc + browser-tree + Xvfb, releases the
    display number and rfbport.

### Rationale: why Xvfb at create, not fully lazy

`real_sdk_factory` (`server/sessions/sdk_factory.py:8-21`) builds the subprocess env once
and hands it to the SDK at spawn; `DISPLAY` cannot be injected into a running `claude`
process afterward. For the agent to use a headed browser, `DISPLAY=:N` must be valid at
spawn, requiring Xvfb to exist at spawn. Fully-lazy Xvfb would require detecting "the
agent is about to launch a browser," which is fragile. Starting lightweight Xvfb at create
— while keeping heavy x11vnc + browser on-demand — is the pragmatic reconciliation. The
feature is off by default, so disabled deployments pay nothing.

## Architecture

Per live session, when enabled:

```
Xvfb :N  -auth <cookie>  (loopback software framebuffer)   ← backs DISPLAY=:N
  ├─ agent's Playwright Chromium (headless:false)              ← agent / open-tab, capped
  └─ x11vnc -rfbport P -localhost -nopw -auth <cookie>         ← lazy on first human attach
        ⇅ RFB over TCP (127.0.0.1:P)
   FastAPI  /ws/vnc?session=<id>   (flow-controlled relay + WS keepalive)
        ⇅ RFB over WebSocket
   noVNC <canvas> in the session view "Browser" tab
```

**No `websockify`, no window manager.** noVNC speaks RFB-over-WebSocket; x11vnc speaks
RFB-over-TCP on localhost. A FastAPI `/ws/vnc` route relays bytes, reusing
`require_ws_identity` — removing a dependency (Principle VII) and keeping Tailscale
identity as the sole network gate (Principle IV). Each Xvfb gets a per-session **xauth
cookie** so other local processes running as the same user cannot snoop another session's
display (Principle II host-level isolation).

## Components

### New modules

- **`server/sessions/x11manager.py`**
  - `DisplayAllocator` — asyncio-locked pool of `(display_number, rfbport)` pairs.
    rfbport drawn from a **dedicated high-port pool** (`CCSWITCH_VNC_PORT_BASE`, default
    6100, range-checked, loopback-only) allocated alongside the display — *not* derived as
    `5900+N` (avoids the IANA VNC range / desktop-host collisions both reviewers flagged).
    `alloc()` / `release()`; raises on exhaustion.
  - `X11Manager` — spawn/supervise per-session Xvfb and x11vnc via
    `asyncio.create_subprocess_exec` (children kept in the service cgroup; **no**
    `start_new_session`, so `KillMode=control-group` is the backstop). Generates the xauth
    cookie, polls X readiness, ensures/stops x11vnc, enforces the running-browser cap.
    **Process teardown:** clean path first (`proc.terminate()` →
    `await asyncio.wait_for(proc.wait(), timeout)` → `proc.kill()`); for the Chromium
    *tree* reuse the existing `server/sessions/adopt.py:terminate_process_tree` rather than
    a single-PID terminate (Chromium spawns zygote/renderer/GPU children).
    `X11Manager` is held on `app.state` and passed into routers/orchestrator explicitly
    (mirroring `build_ws_router(orchestrator)`), not a module global.
  - `sweep_stale()` — boot-time cleanup (see app.py changes).
  - `metrics()` — `{displays_allocated, xvfb_count, x11vnc_count, browser_count,
    pool_capacity, vnc_clients_attached}` for observability.
- **`server/bridge/vnc_ws.py`**
  - `/ws/vnc?session=<id>` route. Auth via `require_ws_identity`. Resolves the session's
    `(rfbport, cookie)`; if the session has no display, closes with a **distinct,
    explanatory** code/reason (not 1008) the frontend maps to specific copy. Lazily ensures
    x11vnc, then runs a **flow-controlled** bidirectional relay: each direction awaits its
    write before reading more, so a slow client cannot cause unbounded buffering in
    uvicorn (OOM guard). Sends periodic **WS ping/keepalive**. Tracks attach count for
    idle teardown; teardown guarded against re-attach races.

### Touched files

- `server/sessions/sdk_factory.py` — **no signature change.** DISPLAY is injected by a
  per-session **closure** built in the orchestrator that wraps the configured factory and
  adds `DISPLAY=:N` (+ `XAUTHORITY`) to the env. This avoids touching the fixed
  `real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume)` signature and all its
  call sites in `driver.py`.
- `server/sessions/orchestrator.py` — **all driver-construction paths handled:**
  - `create_session` (~229): allocate display, start+verify Xvfb, register in new
    registries (mirroring the `_worktrees` dict pattern), build the injecting closure.
  - `ensure_driver` (~584/619) and `_do_rotate` (~882): **reuse** the session's already
    allocated display (look it up; do **not** allocate a new one or drop it) so resumed /
    rotated drivers keep rendering into the same framebuffer.
  - `adopt_session` (~674/699): allocate a fresh display like `create_session`.
  - `close_session` (~285) **and `archive_session` (~919)**: tear down x11vnc + browser
    tree + Xvfb, release display+port — both exit paths, mirroring worktree cleanup
    (`asyncio.to_thread` + `log.exception`).
  - Emit `displayAllocated` / `displayFreed` / `vncAttached` / `vncDetached` on the
    existing event bus.
- `server/config.py` — new settings added to **both** the Pydantic `BaseSettings` branch
  **and** the dataclass fallback branch (the file duplicates every field; omitting the
  fallback causes a silent `AttributeError` on hosts without `pydantic-settings`):
  - `CCSWITCH_ENABLE_XVFB` (bool, **default false**)
  - `CCSWITCH_DISPLAY_START` (int, default 100)
  - `CCSWITCH_DISPLAY_COUNT` (int, default **12**)
  - `CCSWITCH_MAX_BROWSERS` (int, default 6)
  - `CCSWITCH_VNC_PORT_BASE` (int, default 6100)
  - `CCSWITCH_XVFB_RESOLUTION` (str, default `1280x1024x24`)
  - `CCSWITCH_VNC_IDLE_TIMEOUT_S` (float, default 1800) — renamed from the v1
    `*_XVFB_IDLE_TIMEOUT_S`; it governs x11vnc, not Xvfb.
  - Document that `CCSWITCH_REAUTH_DISPLAY` must not overlap the `[START, START+COUNT)`
    range.
- `server/app.py` lifespan:
  - **Startup:** when enabled, run a **preflight check** that `Xvfb`, `x11vnc`, and the
    Playwright Chromium are present; if any is missing, log a loud single-line WARNING and
    keep the feature effectively disabled (fail-loud, mirroring the empty-allowlist guard
    at app.py:73). Then run `X11Manager.sweep_stale()` to remove orphaned `Xvfb`/`x11vnc`/
    Chromium in the managed display range and stale `/tmp/.X{N}-lock` + `/tmp/.X11-unix/X{N}`
    (mirrors the existing `reauth.sweep_stale` / `sweep_orphans` at app.py:99/120).
  - **Shutdown:** add an explicit **"close all live sessions"** step (iterate
    `orchestrator` drivers, `aclose()` each) *before* X11 teardown — `orchestrator.stop()`
    (app.py:105-107) does **not** close sessions today, so X11 teardown would otherwise run
    while drivers are still live.
- **Auto-deploy idle gate (decided — extend):** the test-gated deploy timer currently
  defers a restart only while a session is mid-turn. Extend it to **also defer while a VNC
  client is attached**, so a routine deploy doesn't cold-kill an active human-takeover
  session. (Implementation lives in the deploy gate, referenced here as a required change.)
- **systemd unit `deploy/ccswitch.service`:** add `KillMode=control-group` (explicit),
  raise `TimeoutStopSec` to ~60 s (headed Chromium is slow to exit), and add `MemoryMax`,
  `TasksMax`, `LimitNOFILE=65536`. Children must remain in the service cgroup so the cgroup
  kill is the hard backstop.

### Frontend

- A **"Browser" tab** in the session view (labeled to read as the *agent's* browser — e.g.
  "Browser" with a clarifying subtitle/tooltip "the agent's browser — watch or take over",
  to fix the wrong mental model where an empty tab reads as broken). Lazily mounts a noVNC
  `RFB` instance (**pin noVNC ≥ 1.4**, vendored into `web/`) at `/ws/vnc?session=<id>`.
  Required UX behaviors:
  - **"Agent is driving" indicator** — reuse the existing `status.state === 'working'`
    signal (already on the frontend via `/ws`, shown in `StatusBar`/`WorkingIndicator`) to
    overlay a non-alarming badge over the canvas while the agent is active. Zero new
    backend work; this is what makes shared-cursor tolerable in v1.
  - **Distinct states with copy:** connecting · "Reconnecting…" · disconnected
    ("tap to reconnect", no tight loop against a not-yet-restarted x11vnc) · empty cases:
    (a) display up but framebuffer blank → "The agent hasn't opened a browser yet. When it
    does, you'll see it here." (with the open-tab action), (b) feature disabled on this
    server → "Browser sharing isn't enabled on this server.", (c) no display allocated
    (pool exhausted) → "No display was allocated for this session." Driven by the distinct
    `/ws/vnc` close reasons.
  - **Reconnect:** auto-reconnect on network close (matching `SessionSocket` backoff in
    `web/src/ws/client.ts`); on a *clean* server close (idle teardown) show the
    tap-to-reconnect state instead of looping.
  - **Mobile ergonomics:** noVNC `scaleViewport` **on by default** (fit framebuffer to
    viewport width); a ≥44px "fit / 1:1" toggle outside the canvas; canvas gets full
    viewport height minus the top bar. Document touch mapping: tap = left click, pinch =
    zoom; **swipe-to-scroll is not translated in v1** (acknowledged so it isn't a demo
    surprise); user-facing hint "tap to click, pinch to zoom".
  - **Keyboard / focus:** canvas starts **unfocused** with a "Tap to control" affordance;
    once focused it captures keys. **Two-stage Escape:** first Escape releases canvas focus
    to the tab UI, second Escape uses the existing `SessionView` back-navigation — so
    Escape and the `c` shortcut don't leak into the remote browser.
  - **Open-tab action:** a small control that issues the agent open-tab command (opens a
    tab/URL in the agent's Playwright Chromium), so an idle session isn't a dead end.

## Data flow

1. Create → `DisplayAllocator.alloc()` → `X11Manager.start_xvfb(:N)` (poll ready) →
   injecting closure sets `DISPLAY=:N`,`XAUTHORITY` at spawn.
2. Agent (or open-tab action) runs headed Playwright → Chromium renders into `:N`
   (subject to the running-browser cap).
3. Human opens Browser tab → `/ws/vnc` → lazy `ensure_x11vnc` → flow-controlled RFB relay
   → noVNC paints; "Agent is driving" badge tracks `status.state`.
4. Detach / idle → stop x11vnc (Xvfb + browser persist).
5. Session end (close **or** archive) → kill tree, release `(N, port)`.
6. App shutdown → close all sessions → X11 teardown. Boot → `sweep_stale()`.

## Error handling

- **Pool/browser-cap exhausted:** `alloc()` (or cap) declines; session create proceeds
  **without** a display (graceful degrade, warn-log) — never fail session creation.
- **Xvfb not ready within timeout / binary missing:** treat as "no display"; `/ws/vnc`
  returns the distinct disabled/no-display close reason; session unaffected.
- **Stale X lock files** (crash leftovers): `sweep_stale()` checks the lock's PID
  (`kill -0`) and removes dead-owner locks before reuse, else skips that number.
- **Chromium tree** torn down via `terminate_process_tree`; cgroup kill is the backstop.
- **Relay backpressure:** bounded by await-before-read in both directions.
- **Idle-teardown race:** guarded so a re-attach during teardown restarts cleanly.
- **`require_ws_identity` close-before-accept:** mirror the existing `/ws` ordering
  carefully (middleware.py:199-204 closes before accept on failure).

## Security (Principle IV)

- x11vnc `-localhost -nopw -auth <cookie>`: only reachable via the authenticated FastAPI
  route — Tailscale identity is the sole network gate. No VNC port exposed beyond loopback.
- **Per-session xauth cookie** on Xvfb so co-located processes (same user) can't snoop
  another session's display — closes a host-level cross-session gap (Principle II).
- **Chromium sandbox posture (documented decision):** keep the sandbox **on**; only fall
  back to `--no-sandbox` if user-namespace cloning is restricted on the host, and treat
  that as a conscious downgrade noted in the runbook (the browser may be logged into real
  accounts and is human-controllable).
- **`/dev/shm`:** the open-tab action launches Chromium with `--disable-dev-shm-usage`.
  Known limitation: **agent-launched** Playwright browsers are outside ccswitch's control,
  so this flag can't be forced there — mitigation is documented guidance (recommend the
  flag in agent browser configs) and the shm headroom on the host (~9.7 GB). Flagged as an
  open risk, not silently assumed away.
- Feature **off by default**; enabling is a deliberate per-deployment choice (interactive
  input = real control of a logged-in browser), documented in the quickstart/runbook.

## Testing (Principle V — TDD)

- **Unit**
  - `DisplayAllocator`: alloc/release of `(display, port)`, exhaustion raises, no
    double-alloc, port pool discipline.
  - `X11Manager`: spawn/teardown with a fake subprocess spawner; readiness-poll timeout
    path; terminate→wait→kill; `terminate_process_tree` invoked for browsers; cap
    enforcement; `sweep_stale` removes dead-owner locks only.
  - `/ws/vnc` relay: fake TCP echo — bytes both directions, flow-control under a slow
    reader, clean close on either side, auth-reject path, distinct close reasons for
    disabled / no-display.
  - Orchestrator: display allocated on create, **reused** on `ensure_driver`/`_do_rotate`,
    fresh on `adopt_session`, released on **both** `close_session` and `archive_session`;
    pool exhaustion degrades; injection only when enabled.
- **Integration** (gated on `Xvfb`/`x11vnc`/Chromium present on host/CI)
  - Real Xvfb + x11vnc smoke; RFB handshake bytes end-to-end through `/ws/vnc`.
  - **Smoke through the real `tailscale serve` path**, not just localhost, before
    declaring done (long-lived binary WS behavior under the userspace proxy).
- Run server tests from a worktree with `PYTHONPATH=$(pwd)/src`, the prebuilt main
  `.venv`, and pinned `CCSWITCH_TAILNET_ALLOWLIST` or `CCSWITCH_DEV=1` per conventions.

## Provisioning / runbook (must ship with the feature)

1. **Host packages:** `apt install xvfb x11vnc`; `playwright install --with-deps chromium`
   (needs root). x11vnc and Chromium are **not installed on the host today** — the worktree
   auto-deploy cannot `apt install`, so these are out-of-band, like `/etc/ccswitch/ccswitch.env`.
2. **Preflight** verifies all three when `CCSWITCH_ENABLE_XVFB=true`; fail-loud if missing.
3. **Boot sweep** of stale locks/sockets/orphans in the managed range.
4. **systemd hardening** (`KillMode`, `TimeoutStopSec`, `MemoryMax`/`TasksMax`/`LimitNOFILE`).
5. **Auto-deploy idle gate** extended to defer on attached VNC.
6. **Observability:** `X11Manager.metrics()` surfaced (admin/health) + a documented
   `pgrep -af 'Xvfb|x11vnc'` leak-check line.

## Constitutional notes

- **I (no fork):** no Claude-binary changes; we set `DISPLAY`/`XAUTHORITY` in its env.
- **II (isolation):** display number + rfbport + xauth cookie are per-session resources
  allocated/reclaimed like `CLAUDE_CONFIG_DIR`, PTY, worktrees; the cookie closes a
  host-level cross-session snoop gap.
- **III (verify real):** integration tests exercise real Xvfb/x11vnc, a real RFB
  handshake, and the real `tailscale serve` path.
- **IV (security-first):** loopback-only VNC, Tailscale sole gate, off by default,
  documented sandbox posture.
- **VI (single source of truth):** one `X11Manager`/`DisplayAllocator`, passed explicitly;
  no parallel display bookkeeping; reuses `terminate_process_tree`.
- **VII (modern deps):** no `websockify`; Xvfb/x11vnc are OS packages in the deploy layer,
  noVNC vendored & version-pinned — Python dependency graph untouched.

## Phasing

1. `DisplayAllocator` + `X11Manager` (Xvfb only) + config (both branches) + orchestrator
   alloc/inject/reuse/cleanup across **all** lifecycle paths + preflight + boot sweep +
   systemd hardening → agent gets a working headed browser, no VNC. Fully testable headless.
2. `/ws/vnc` flow-controlled relay + lazy x11vnc + idle teardown + deploy idle-gate
   extension → RFB bytes flow; relay tested against fake + real x11vnc + tailscale path.
3. noVNC "Browser" tab: scaleViewport, state copy, reconnect, agent-driving indicator,
   keyboard/focus handling, open-tab action → human takeover.

## Open questions

None blocking. Residual risks tracked above: agent-launched-browser `--disable-dev-shm-usage`
(documented guidance only), and Chromium sandbox posture (host-dependent, documented).
