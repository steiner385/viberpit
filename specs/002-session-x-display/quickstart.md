# 002 — Virtual Browser: Operator Quickstart & Runbook

Feature 002 gives each managed `claude` session its own virtual X display (Xvfb) so the
session's agent can run a headed browser, and a human can watch or take over that browser
from the web UI's **Browser** tab via noVNC.

**Off by default.** Enabling it is a deliberate per-deployment decision — the remote VNC
canvas gives real interactive control of a browser that may be logged into real accounts.

---

## 1. Host provisioning (one-time, needs root)

The auto-deploy cannot `apt install` packages, so these steps are out-of-band — the same
as editing `/etc/ccswitch/ccswitch.env`.

```bash
# X virtual framebuffer + VNC server
sudo apt install xvfb x11vnc

# Playwright's Chromium (with its own shared-lib bundle)
playwright install --with-deps chromium
```

`x11vnc` and Chromium are **not installed on the host by default**. Xvfb may already be
present on a desktop/GUI host; verify with `which Xvfb`.

---

## 2. Enabling the feature

Add the following block to `/etc/ccswitch/ccswitch.env` (root-only, `chmod 600`):

```env
# --- Virtual browser (feature 002) ---
CCSWITCH_ENABLE_XVFB=true

# Display pool: displays :100–:111 (12 slots)
CCSWITCH_DISPLAY_START=100
CCSWITCH_DISPLAY_COUNT=12

# Max simultaneously running Chromium browsers (across all sessions)
CCSWITCH_MAX_BROWSERS=6

# Loopback RFB port pool (6100–6111, one per display slot)
CCSWITCH_VNC_PORT_BASE=6100

# Xvfb framebuffer geometry
CCSWITCH_XVFB_RESOLUTION=1280x1024x24

# Seconds of VNC idle before x11vnc tears down (Xvfb and browser persist)
CCSWITCH_VNC_IDLE_TIMEOUT_S=1800
```

All values above are the defaults — only `CCSWITCH_ENABLE_XVFB=true` is required to opt
in. The rest can be omitted unless you need to change them.

> **Display range conflict:** ensure `CCSWITCH_REAUTH_DISPLAY` (default `:0`) does not
> fall within `[CCSWITCH_DISPLAY_START, CCSWITCH_DISPLAY_START + CCSWITCH_DISPLAY_COUNT)`.

Restart the service after editing:

```bash
sudo systemctl restart ccswitch.service
```

---

## 3. Preflight check (startup behavior)

When `CCSWITCH_ENABLE_XVFB=true`, the server runs a preflight check at startup that
verifies `Xvfb`, `x11vnc`, and `xauth` are all present on `PATH`. If any is missing, the
server logs a single loud WARNING and the feature stays effectively disabled
(`app.state.x11` is `None`). The server starts normally — session management continues
without virtual browser support.

**The server does not crash.** Missing binaries are a fail-loud, graceful degradation.

Check the startup log:

```bash
journalctl -u ccswitch.service -n 50 | grep -i "xvfb\|x11\|preflight"
```

A successful preflight looks like:

```
INFO  x11manager: preflight ok — Xvfb, x11vnc, xauth found; sweep complete
```

A missing binary:

```
WARNING  x11manager: preflight failed — x11vnc not found; virtual browser disabled
```

---

## 4. How it works (brief)

```
Xvfb :N  -auth <cookie>          ← per-session virtual framebuffer
  ├─ agent's Playwright Chromium  ← agent's headed browser (headless:false)
  └─ x11vnc -rfbport P -localhost ← lazy; starts on first human attach
        ⇅ RFB over TCP (127.0.0.1:P, loopback only)
   FastAPI  /ws/vnc?session=<id>  ← flow-controlled relay, Tailscale-gated
        ⇅ RFB over WebSocket
   noVNC <canvas> in Browser tab
```

- **Xvfb starts at session create** (not lazily) so `DISPLAY=:N` is valid the moment the
  agent might launch a browser. Cost: ~30–50 MB RAM per live session.
- **x11vnc starts lazily** on the first human attach to `/ws/vnc`, torn down after
  `CCSWITCH_VNC_IDLE_TIMEOUT_S` of VNC idle. Xvfb and any agent browser persist across
  VNC detach.
- **Chromium is not pre-launched.** It appears when the agent's Playwright starts it, or
  when a human triggers the "Open a browser" action (POST
  `/api/sessions/{id}/browser/open`).
- **Display and RFB port** are paired per session: display `:N` uses RFB port
  `CCSWITCH_VNC_PORT_BASE + (N - CCSWITCH_DISPLAY_START)`, allocated from the pool.
- **Session end** (close or archive) kills x11vnc + the Chromium process tree + Xvfb,
  then releases the display number and port back to the pool.

---

## 5. Observability and leak checks

### Metrics endpoint

```
GET /api/x11/metrics
```

Requires Tailscale identity (same gate as the rest of the API). Returns:

```json
{
  "displays_allocated": 3,
  "xvfb_count": 3,
  "x11vnc_count": 1,
  "browser_count": 2,
  "vnc_clients_attached": 1,
  "pool_capacity": 12
}
```

`displays_allocated` should equal `xvfb_count` for healthy live sessions. A
`xvfb_count` below `displays_allocated` means one or more Xvfb processes crashed.

### Process leak check (on the host)

```bash
pgrep -af 'Xvfb|x11vnc'
```

Under normal operation you expect one `Xvfb` per live session, and at most one `x11vnc`
per session that has an active VNC client. After a service restart with no active sessions,
this command should return nothing.

### Boot sweep

On startup the server runs `X11Manager.sweep_stale()`, which removes:
- stale `/tmp/.X{N}-lock` files whose owning PID no longer exists
- stale `/tmp/.X11-unix/X{N}` sockets
- orphaned `Xvfb`/`x11vnc`/Chromium processes in the managed display range

This covers crash leftovers from an unclean previous shutdown.

---

## 6. Deploy interaction

### Auto-deploy idle gate

The 2-minute auto-deploy timer defers a service restart while a session is mid-turn **or
while a VNC client is attached**. This prevents a routine deploy from cold-killing an
active human-takeover session.

The deploy script reads the auth-exempt `/healthz` endpoint (not `/api/x11/metrics`, which
requires a Tailscale identity) and checks the `"vncAttached"` boolean field added to its
response. The `/api/x11/metrics` route remains available for operator observability.

The deferral is capped by `CCSWITCH_DEPLOY_MAX_DEFER_SEC` (set in `ccswitch.env` or the
deploy script). Once the cap is reached the restart proceeds regardless.

### systemd hardening (`deploy/ccswitch.service`)

The service unit is configured with:

```ini
KillMode=control-group
TimeoutStopSec=60
MemoryMax=12G
TasksMax=512
LimitNOFILE=65536
```

`KillMode=control-group` ensures that Xvfb, x11vnc, and Chromium children — spawned as
subprocesses of uvicorn — are reaped on service stop/restart. `TimeoutStopSec=60` gives
headed Chromium time to exit cleanly before SIGKILL.

Standard operate commands:

```bash
systemctl status ccswitch.service
journalctl -u ccswitch.service -f
sudo systemctl restart ccswitch.service
```

---

## 7. Security notes

- **x11vnc is loopback-only** (`-localhost -nopw`). It binds to `127.0.0.1` only and
  accepts no password because the FastAPI `/ws/vnc` route (Tailscale-gated) is the sole
  network gate. No VNC port is exposed beyond loopback.
- **Per-session xauth cookie**: each Xvfb gets its own MIT-MAGIC-COOKIE-1 entry so
  co-located processes running as the same user cannot snoop another session's display.
- **No new exposed ports.** All access flows through the existing authenticated FastAPI
  app over Tailscale.
- **Enabling is a deliberate choice.** The Browser tab gives real interactive control of
  a browser that may be logged into real accounts. Only enable on deployments where you
  intend to use it.

---

## 8. Known limitations and residual risks

### (a) `--disable-dev-shm-usage` for agent-launched browsers

The managed "Open a browser" action (POST `/api/sessions/{id}/browser/open`) launches
Chromium with `--disable-dev-shm-usage` to avoid `/dev/shm` exhaustion.

**Agent-launched Playwright browsers are outside ccswitch's control** — the agent calls
Playwright directly and may not pass this flag. On a host with limited shared memory this
can cause Chromium to crash. Mitigation: configure your agent's Playwright setup to
include `--disable-dev-shm-usage` in its launch args. The host has ~9.7 GB of shm
headroom, so this is unlikely to be a problem at low concurrency but is worth noting for
agents that open many tabs.

### (b) Chromium sandbox posture

Keep the Chromium sandbox **on** (the default). Only use `--no-sandbox` if
user-namespace cloning is restricted on the host (e.g., some container runtimes, or
Ubuntu 23.10+ with `kernel.apparmor_restrict_unprivileged_userns=1`), and treat that as
a conscious downgrade: the virtual browser may be logged into real accounts and is
human-controllable from the web UI. Symptom of the restriction: the managed launch
FATALs at startup with `No usable sandbox!` and renders nothing.

The **secure** fix is a host change — enable unprivileged userns:
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. If you can't change the
host, set `CCSWITCH_CHROMIUM_NO_SANDBOX=1` to make the managed launch pass `--no-sandbox`.
If you disable the sandbox, document it in your deployment notes and restrict VNC access
to trusted users only.

Note: managed launch now mints a fresh isolated `--user-data-dir` per browser and runs a
short post-spawn liveness check (`CCSWITCH_BROWSER_LIVENESS_S`, default 0.3s), so a
FATAL-at-startup launch reports failure instead of a phantom `browser_count`.

### (c) Shared cursor — no hard locking in v1

The agent and a connected human share one framebuffer and one cursor. There is an
**"Agent is driving" badge** in the Browser tab UI (shown while the session's agent is
in a working state), but no hard lock preventing the human from clicking while the agent
is mid-action. This is a v1 limitation; treat the badge as an advisory signal.
