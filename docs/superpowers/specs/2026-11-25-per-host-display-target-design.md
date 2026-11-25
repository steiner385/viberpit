# Per-host display target: a host's sessions draw on another host's desktop

Status: design, awaiting approval · 2026-11-25

## Intent

The operator sits at dobby. Sessions on rocky (and on future runners) open GUI programs: headed Playwright,
Electron apps, browsers and other desktop tools. Today those go to rocky's private Xvfb displays, and the
operator sees them only through the VNC panel in the web UI. The operator wants each fleet host to have a
**default display target** for its sessions' GUI windows. For example: *rocky → dobby's primary desktop*.
Every program a rocky session starts then shows up as a real, separate window on dobby's screen.

## Operator decisions (2026-11-25)

- **Scope: any GUI the agent runs**, not only browser windows ViberPit launches.
- **Transport: real windows through waypipe** (Wayland-native), plus an SSH-forwarded X11 display for
  X-only programs. A full-screen viewer window (VNC) was rejected: the operator wants individual windows.
- **Windows: a Windows PC as the *viewer*** (Linux hosts' windows appear on a Windows desktop). No such
  PC exists yet, so this is **designed but not built**. Linux X11 and Wayland desktops ship now.

## Goals

1. A per-host setting, **display target**, editable in Settings → Fleet. Values:
   - `local` (default, NULL): today's behaviour. Native when the operator's browser is on this host,
     else a pooled Xvfb display.
   - `host:<name>`: the primary desktop of another fleet host, e.g. `host:dobby`.
2. A session born on a host whose target is `host:<name>` gets a new display mode, `remote`. Its process
   env points at a bridge into that desktop, so any GUI it spawns opens there as real windows:
   - `WAYLAND_DISPLAY`: a waypipe socket on the session host, for Wayland-capable programs.
   - `DISPLAY` + `XAUTHORITY`: an SSH-forwarded X11 display on the session host, for X-only programs.
3. The target desktop may be **X11 or Wayland**:
   - Wayland target (dobby today): both legs.
   - X11 target: the X11 leg only, so `WAYLAND_DISPLAY` is not set.
4. When the target desktop is unavailable (logged out, host down, bridge fails), the session
   falls back to a virtual display, and `display_fallback_reason` records why, exactly as native
   fallback does today. A session never starts without a display because of this feature.
5. ViberPit-managed browsers ("Open a browser", dev-server preview) for a `remote` session open on the
   target desktop too, through the same env.

## Non-goals

- A Windows-PC target (designed in §7, not built).
- macOS targets.
- Moving existing live sessions between displays. The decision is made at session birth, like
  `display_mode` today. Changing a host's target affects new sessions only.
- Per-session choice of *which* remote desktop. Per session, only the existing `virtual` override applies.
- GPU-accelerated forwarding tuning (dmabuf/video encoding flags). Use waypipe defaults; tune later if
  needed.

## Anti-goals: a failure even if it "works"

- **A bridge left open with nothing using it.** While it is up, processes on the session host can draw on
  the operator's desktop and, over X11, read its input and screen. The bridge must exist only while at
  least one live `remote` session needs it.
- A rocky session whose GUI silently goes *nowhere* (env points at a dead socket) instead of falling back.
- The target desktop's X cookie or any SSH key appearing in a transcript, log line, API response or the
  repo.
- A target misconfiguration (unknown host, self-target, target with no desktop) breaking session spawn on
  that host.
- Forwarded X11 displays colliding with the Xvfb pool range (`display_start`…+`display_count`), whose
  orphan sweep would kill them.

## Constraints

- Constitution v1.2.0: III (verify against the real waypipe 0.11 and real SSH, not docs), IV (security
  first: see Anti-goals), VI (one source of truth: extend `display_policy.resolve_display_mode` and
  `orchestrator._display_env`, never a parallel decision path), VII (waypipe is the vetted dependency;
  it is in Ubuntu's archive, 0.11.0, on both dobby and rocky).
- DB: new columns nullable, with an Alembic revision chained off the newest one. `Host.display_target`
  (String, NULL = `local`) and `Session.display_target_host` (String, NULL unless `remote`), `Host.display_ssh_host` (String, nullable).
- New settings take the `VIBERPIT_` prefix in both `Settings` classes in `server/config.py`.
  Kill switch: `VIBERPIT_DISPLAY_BRIDGE_ENABLED` (default true). When it is false, every host behaves as
  `local`.
- Rolling compatibility: a daemon predating `remote` mode must not crash on a row carrying it. Today's
  `resolve_display_mode` already collapses unknown modes to `auto`; check the read paths do too.
- Recognised facts about the hosts (checked 2026-11-25):
  - dobby is GNOME/mutter on Wayland. Its Xwayland `:0` listens on local sockets only (`-listenfd`, no
    TCP) and needs the rotating per-login cookie. `desktop_session.resolve_desktop_session` already
    resolves both the cookie and the `WAYLAND_DISPLAY`.
  - dobby → rocky SSH works non-interactively, and rocky's sshd has `X11Forwarding yes`. Neither host has
    waypipe installed yet.
  - rocky uses `/tmp/.X11-unix/X99`, `X101` and `X109`: Xvfb pool and VM displays.

## Approaches considered

- **A. Viewer window.** The target desktop shows a live VNC/SPICE viewer of the session host's Xvfb.
  Safest, and nothing new on the network, but one window holds the whole screen. *Rejected by the
  operator: wants real per-app windows.*
- **B. SSH X11 only.** Real windows for X11 programs through an SSH-forwarded display into the target's
  Xwayland. Simple, but Wayland-native programs (and Chromium/Electron under Ozone Wayland) would go
  through XWayland, and it is X11-insecure by nature.
- **C. waypipe + SSH X11 fallback.** *Chosen.* Wayland programs get waypipe's per-window protocol proxy;
  X-only programs still get leg B. It is the most work, but every GUI gets a native-feeling window on a
  Wayland desktop.

## The design

### 1. Setting and policy

- `Host.display_target`:
  - Validated by the fleet API (`server/api/fleet.py` `_PATCHABLE`, wire name `displayTarget`). It must
    be NULL/`local`, or `host:<name>` naming another enabled `Host` row that is not itself.
  - Surfaced in the Settings → Fleet host table as a select: "This host (default)" plus one entry per
    other host, "<name>'s desktop".
- The **target daemon** (dobby) must also be *willing*: a new daemon setting,
  `VIBERPIT_DISPLAY_BRIDGE_ACCEPT_FROM` (comma-separated host names, default empty). A host can only be
  made to draw on a desktop whose owner opted in on that machine. This is the consent boundary for
  Anti-goal 1.
  - **Builder:** validate it in the target daemon. The edge additionally rejects (warns, does not block
    save) a target that does not advertise acceptance, through `/info`.
- `display_policy.resolve_display_mode` gains a `remote_available: bool` input and returns a new
  `DISPLAY_REMOTE` constant. Precedence, in order:
  1. Explicit `virtual` → virtual.
  2. Explicit `native` with the native display available → native.
  3. `auto` and the client is local to the runner, with native available → native (today's rule, so an
     operator physically at rocky still gets rocky's screen).
  4. The host target is `host:<x>` and the bridge to `x` is available → **remote**.
  5. Otherwise → virtual. Set `display_fallback_reason` when 2 or 4 were wanted but unavailable, reusing
     the `native_fallback` shape (extend it, don't fork it).
- `Session.display_target_host` persists *which* desktop, so resume/adopt re-attach to the same one,
  consistent with "a prior record's persisted decision wins" (`_display_decision`).

### 2. The bridge (who runs what)

The **target host's daemon** (dobby) owns the bridge: it holds the live desktop, the cookie resolver and
the SSH trust to the session host. Per (target, source) pair there is at most one bridge, a supervised
process group run by a new `DisplayBridge` component on the target daemon. It is the sibling of
`NativeDisplay` and reuses its resolver, so there is no second desktop discovery.

- **Wayland leg:**
  - On the target, run `waypipe client` listening on a private socket.
  - Run `ssh -R <source-side socket>:<client socket>` to the source host, which runs `waypipe server`.
    That creates a Wayland socket on the source, e.g. `$XDG_RUNTIME_DIR/wayland-viberpit-<target>`, for
    any number of clients.
  - **Builder:** verify with the real waypipe 0.11 how to run the server side as a long-lived
    multi-client socket rather than per-command (`waypipe server -- cmd`). Some modes need one waypipe
    per client process. If 0.11 cannot multiplex, the fallback is a per-session waypipe server launched
    by the *source* daemon at session spawn over the same forwarded client socket. Record what was found
    in this doc.
- **X11 leg:**
  - In the same SSH connection, run `-R /tmp/.X11-unix/X<N>:<target X socket>`, with N from a reserved
    range outside the Xvfb pool (new setting `VIBERPIT_DISPLAY_BRIDGE_X_START`, default 150; validated
    against the pool at startup like `native_display` does).
  - Write the target's live cookie into a bridge-private `XAUTHORITY` file on the source over the SSH
    session, never as an argv value.
  - Re-push the cookie when the target's login rotates it. The resolver already notices this within its
    TTL.
- **SSH:**
  - Use the source host's existing `Host.ssh_host`.
  - Set `ServerAliveInterval`, `ExitOnForwardFailure=yes` and `StreamLocalBindUnlink=yes`, with
    BatchMode only (no prompts).
  - No new keys: the dobby → rocky trust already exists. Document it in the runbook as required.
- **Lifecycle: the edge brokers it, as desired state, not refcounts** *(design call: daemons have no
  daemon→daemon RPC today; every `DaemonClient` lives in the edge, and peer certs would be a new trust
  surface).*
  - The target daemon exposes one idempotent RPC, `display_bridge.ensure(sources: list[str])`. It brings up
    listed sources and starts a grace timer (default 60 s) for unlisted ones.
  - The edge computes the desired set from the DB (live `display_mode='remote'` sessions grouped by
    `display_target_host` → source host). It calls `ensure` on spawn and archive, and on the fleet probe
    tick.
  - An edge restart loses nothing: the next tick re-asserts the set. A target that hears nothing for 3
    probe intervals treats its set as empty (fail closed, Anti-goal 1).
  - Also torn down on desktop logout and daemon shutdown (`aio.cancel_and_reap`). A crashed bridge
    restarts with backoff while desired.
- **Spawn waits for the bridge, briefly** *(design call)*.
  - The edge passes the source host's target in `create_session` params, like `display`/`client_node`
    today. An older daemon ignores it and stays `local`.
  - The edge calls `ensure` and waits up to `VIBERPIT_DISPLAY_BRIDGE_START_TIMEOUT_S` (default 15 s). If
    the bridge is still down, the session is born virtual with
    `display_fallback_reason="bridge_unavailable"`.
  - Queued/held sessions resolve at actual spawn. A persisted remote decision wins on resume/adopt and
    re-asserts the bridge.
- **SSH identity** *(design call)*.
  - The bridge logs in as the same Unix user the source daemon runs sessions as; otherwise sessions
    can't open the forwarded sockets or the 0600 `XAUTHORITY`.
  - New nullable `Host.display_ssh_host`, falling back to `ssh_host`, validated with `SSH_HOST_RE`.
  - The source daemon treats sockets owned by another uid as not live, so the chip shows "wrong user"
    instead of failing silently.
- **Env injection:** `orchestrator._display_env` gains the `remote` branch. It returns the bridge's env
  (`WAYLAND_DISPLAY`, plus `XDG_RUNTIME_DIR` when the socket lives there, plus `DISPLAY` and
  `XAUTHORITY`) only while the source daemon's local liveness check passes: the sockets exist and accept
  a connection. Otherwise it returns `{}`, the same degrade native gives today.
  - **Builder:** decide whether a mid-session bridge loss should notify the session (a system event, as
    display fallback is surfaced today). Required: no silent dead socket at *spawn*.
- **Managed browsers:** for `remote` sessions, `open_browser` launches Chromium on the **source** host
  with the bridge env and `--ozone-platform=wayland` (X11 leg when the target is X11-only). The windows
  appear on the target through the bridge. No new browser launcher; reuse X11Manager/NativeDisplay's
  argv builder.

### 3. Status and UI

- `/info` on each daemon advertises `displayBridge: {accepts: [...], active: [{source, since, legs}]}`.
  The edge probe stores what it needs for the Fleet table, following the `engines`/`platforms`
  advertisement pattern.
- The Fleet host table shows the target select and, when it is not `local`, a status chip: "→ dobby:
  bridge up", "→ dobby: desktop logged out", "→ dobby: not accepted", "→ dobby: wrong user". *(Design call: a
  **locked** screen counts as available, the same as native mode today. Windows open behind the lock and
  are there on unlock. No new lock-state probe.)*
- The session header already shows native/virtual. Add "on dobby's desktop" for `remote`, with the
  fallback reason when it went virtual. The VNC panel is hidden for `remote` sessions, as it is for
  native.

### 4. Security summary

- Consent: the target daemon's `ACCEPT_FROM` allowlist. The edge setting alone is not enough.
- Exposure window: edge-asserted desired state with a fail-closed timeout, so the bridge is up only while a `remote` session is live.
- Secrets: the cookie goes over SSH stdin into a 0600 file in a 0700 dir. It never appears in argv, logs
  or API. Bridge log lines name hosts and legs only.
- X11 is inherently able to keylog the target's other X clients. Wayland-native programs over waypipe
  cannot. Document this in the Fleet UI help text and the runbook. A per-host option to disable the X11
  leg (`VIBERPIT_DISPLAY_BRIDGE_X11=false`) gives Wayland-only targets a stronger posture.

### 5. Ops

- Runbook: `docs/runbooks/display-bridge.md`, covering:
  - `apt install waypipe` on both hosts. This is an operator step; the deploy controller does not install
    apt packages.
  - The required SSH trust direction (target → source).
  - Setting `VIBERPIT_DISPLAY_BRIDGE_ACCEPT_FROM=rocky` on dobby.
  - Choosing the target in Fleet settings.
  - Troubleshooting from the status chip states.
- `deploy/README.md` gets a pointer.

### 6. Testing

- Pure policy: exhaustive `resolve_display_mode` precedence tests, including every fallback reason.
- `DisplayBridge`: a fake-spawn unit test for argv, `ensure` desired-state + grace + fail-closed silence, restart backoff, cookie re-push on
  rotation, and that the cookie never reaches argv or log records.
- Fleet API validation: self-target, unknown host, wire round-trip. Web: the select + chip in
  `FleetSection` (remember the manual `@/api/hooks` mocks; run the full `npm run test`).
- **Real verification (Principle III), dobby + rocky:**
  - A rocky session with target dobby runs `foot`/`gtk4-demo` (Wayland), `xeyes` (X11) and headed
    Chromium; each appears as its own window on dobby's desktop.
  - Logging out falls back cleanly.
  - The bridge disappears ~60 s after the last session ends.
  - Record the evidence (screenshots via the existing Xwayland-cookie forensics, per memory) in the PR.

### 7. Windows-PC target (designed, not built)

- A future `display_target` kind, `windows:<ssh-host>`. Windows has no Wayland compositor waypipe can
  target, so this is **X11 only**:
  - An X server on the PC (VcXsrv or X410) listening locally.
  - Windows OpenSSH Server.
  - The bridge runs from the *edge or source* side as `ssh -R` into the PC instead of from a target
    daemon, since no ViberPit daemon runs on Windows (per the 2026-11-03 host-platforms design).
- Wayland-native programs on such a session run with `WAYLAND_DISPLAY` unset, so they fall back to X11
  or headless.
- Nothing for it ships now. The `display_target` string grammar (`local` | `host:<name>`) leaves room for
  the `windows:` prefix without a migration. No placeholder code, enum value or disabled flag is added
  (Principle VI).

## Left to the builder

- Exact waypipe 0.11 invocation and multi-client behaviour (§2, verify first, before other work).
- Socket paths and directory layout on the source host.
- Grace period and backoff constants.
- Whether mid-session bridge loss emits a session event (§2).
- Splitting into PRs. Suggested order: (1) waypipe spike/verification notes; (2) policy + columns +
  Fleet API/UI behind the kill switch; (3) `DisplayBridge` + RPC + env injection; (4) runbook + real
  verification.
