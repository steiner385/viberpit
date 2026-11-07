---
slug: remote-desktop-vnc-x11
parent: operator-experience
title: Remote Desktop (VNC / X11 Browser Bridge)
state: live
links:
  code:
    - server/bridge/vnc_ws.py
    - server/ipc/x11_rpc.py
    - web/src/components/BrowserTab.tsx
    - web/src/vnc/vncClient.ts
    - web/src/vnc/keyRelay.ts
    - web/src/vnc/clipboardPaste.ts
    - web/src/vnc/useZoom.ts
    - server/sessions/native_display.py
    - server/sessions/display_policy.py
    - server/sessions/dev_servers.py
    - web/src/features/sessions/browser/DevServers.tsx
  prs: [130, 331, 332, 427, 428, 399, 341, 204, 339, 373]
---

# Remote Desktop (VNC / X11 Browser Bridge)

Watch and interact with a session’s browser remotely, including login flows, from the desktop or mobile interface.

**Problem** — Some session work needs a real GUI: a browser the agent drives, or a login
flow (account re-auth) that only completes in a graphical browser. On a headless server,
and especially from a phone, there is no way to see or interact with that GUI.

**Target users** — The operator whose sessions launch a managed browser (browser-driven or
GUI tooling inside a session), and the operator completing an account re-auth device/login
flow that requires a graphical browser — both viewed from any tailnet device including a
phone.

**Desired outcome** — Each session can run on a virtual X display (Xvfb) with a browser,
exposed to the web UI as a live noVNC canvas: the RFB stream is relayed from the daemon-side
`x11vnc` to the browser over a Tailscale-identity-gated WebSocket. The operator can watch,
click, type, paste from the clipboard, and zoom — including on mobile with a soft-keyboard
toggle. The same VNC path renders the re-auth browser so a login can be completed from a
phone.

**Success metrics** — (unknown — needs owner input). Reliability signals in code: the first
attach isn't lost to the x11vnc spawn→RFB-bind race (bounded connect retry, #427/#399);
orphaned Xvfb/x11vnc/chrome are reaped at startup (#257); the VNC path works across the
daemon→edge split in remote mode (#204).

**Non-goals** — A full remote desktop of the host — this is a per-session virtual browser
display, not the operator's whole desktop. Being on by default (the per-session virtual
browser with human takeover is opt-in, off by default, #130). Exposure beyond the tailnet
(the WS relay is identity-gated).

**Notes** — Feature 002 introduced the per-session virtual browser with human takeover
(#130), off by default. The re-auth flow runs its browser on a virtual X display viewable
via web VNC (#331), with a large resizable viewer (#332) and clipboard paste for setup
tokens (#373). Managed-browser hardening covers sandbox flags, per-launch profiles, liveness
(#428) and an opt-in CDP debug port with a persistent profile (#341). In remote mode the
VNC/x11 relay is bridged across the daemon↔edge boundary so it never assumes edge and daemon
share loopback (#204). Mobile gets a soft-keyboard toggle for virtual X sessions (#339). On
insecure origins the paste button falls back to a capture field (#541).

**Evidence** —
- Relay: `server/bridge/vnc_ws.py` (WebSocket↔x11vnc RFB byte relay, identity-gated),
  `server/ipc/x11_rpc.py` (daemon-side `/vnc` endpoint + x11 RPCs across the edge split).
- Web viewer: `web/src/components/BrowserTab.tsx`, `web/src/vnc/vncClient.ts`,
  `web/src/vnc/keyRelay.ts`, `web/src/vnc/clipboardPaste.ts`, `web/src/vnc/useZoom.ts`.
- PRs: #130 (per-session virtual browser, off by default), #331/#332 (re-auth browser on
  virtual X, viewable/resizable via VNC), #427/#399 (RFB relay connect retry / bind race),
  #428/#341 (managed-browser hardening, CDP debug port), #204 (VNC path across daemon→edge
  in remote mode), #339 (mobile soft-keyboard toggle), #373 (VNC clipboard paste for setup
  token).

## Native display routing (2026-09-06)

A session created from the runner host's **own desktop** does not get a virtual display:
its `DISPLAY` is the host's real X display (discovered at runtime; `VIBERPIT_NATIVE_DISPLAY` pins one) and
"Open a browser" launches Chromium there. Every other client — any other tailnet device,
automations, the assistant — keeps the pooled Xvfb display and VNC relay.

**Detection.** The create route resolves the requesting browser's tailnet node with
`tailscale whois` (the same peer-verified path identity uses) and the daemon that spawns
the process compares it with its own `self_host_name()`. Native only when they match
*and* the X socket (`/tmp/.X11-unix/X<n>`) exists on that host. A local client also
*prefers* its own host for placement so the rule can hold across the fleet; an explicit
host choice still wins. The decision is made once at spawn, persisted as
`Session.displayMode`, and honoured on rehydrate. Attaching later from another device
does not move a session.

**Override.** `POST /api/sessions` accepts `display: "auto" | "native" | "virtual"`.

**Requirements / limits.** The daemon runs as the desktop user. No `SI:localuser` grant and no
`xhost` step are needed: `server/sessions/desktop_session.py` finds the live X server from `/proc`
(Xorg, or the rootless Xwayland of a Wayland session), reads its per-login cookie from argv/environ,
proves it with a real X11 handshake, and exports `DISPLAY` + `XAUTHORITY` (plus `WAYLAND_DISPLAY` /
`XDG_RUNTIME_DIR` on Wayland). It only trusts X servers and sockets owned by the daemon's uid or
root, and re-resolves on a 5 s TTL, so a relogin (which rotates the cookie) is picked up
automatically; stale cookie files and `/tmp/.X11-unix/X*` sockets are ignored. Empty
`VIBERPIT_NATIVE_DISPLAY` (default) = auto-discover; `:N` pins a display.
`VIBERPIT_NATIVE_BROWSER_PLATFORM=auto|wayland|x11` picks the Chromium backend: `auto` runs
Wayland-native on a Wayland desktop and X11 otherwise; forcing `wayland` on an X11-only desktop
falls back to x11. Virtual (Xvfb) Chromium is always pinned `--ozone-platform=x11` with the
Wayland vars scrubbed so it cannot escape onto the real desktop. When a local session lands on a
virtual display instead, `Session.displayFallbackReason` (`displayFallbackReason` in the API,
migration 0080) records why and the session card shows it as a badge tooltip. The reauth
host-screen fallback uses the same resolver (X11 via Xwayland; `VIBERPIT_REAUTH_DISPLAY` empty =
auto-discover). Authenticated `GET /api/x11/metrics` exposes `nativeDisplay`
(`display`, `available`, `kind`, `reason`, `browsers`); `/healthz` does not (unauthenticated).
See `docs/runbooks/onboarding-a-session-runner.md` § "Wayland desktop hosts". `VIBERPIT_NATIVE_DISPLAY` must not fall inside
the Xvfb pool range or native routing disables itself with a warning (the orphan sweep
reaps that range). Behind `tailscale serve` (no `X-Real-IP`) locality resolves to unknown →
virtual. Native routing is effectively **Claude-only** today: only
`ClaudeAgentDriverFactory` is built from the orchestrator's `_session_factory`, the one seam
that injects `DISPLAY`, so a Codex session resolved native takes no Xvfb lease *and* gets no
`DISPLAY` — pass `display: "virtual"` (or use the kill switch) for Codex sessions launched
from a runner's own desktop. In fleet mode an explicit `display: "native"` from a *remote*
requester skips the locality proof but not placement, so under auto placement its windows
land on whichever desktop-capable host ranks first, not necessarily the one the requester
expected — pin `host` to be sure. Kill switch: `VIBERPIT_NATIVE_DISPLAY_ENABLED=false`.

## Dev servers (D-18)

When a session starts a dev server (Vite, Next, `python -m http.server`, and so on), the **Browser tab** lists it
under "Dev servers" with its port, the command that started it and the page title. Pick **Open** and the page
opens in the way that works for where you are:

- **You are on the session's own computer.** The page opens as a normal link in your browser, at the address the
  server listens on (e.g. `http://127.0.0.1:<port>/`).
- **The server is open to the network** (it listens on `0.0.0.0`, for example `vite --host`). The link uses the
  host's tailnet IP address, so any device on your tailnet can open it directly.
- **Anything else** (a loopback-only server on another host, or your phone). ViberPit starts a browser on a
  virtual display on the session's host, opens the page there, and switches the tab to the live VNC view. This
  works for sessions started from the host's own desktop too: the preview gets its own virtual display, and the
  session itself is left alone. When the session's host has no virtual displays (X11 is off there), there is no
  way to show such a server on this device: the row says "Not available from this device" and has no Open.

A server that listens on `0.0.0.0` is reachable from your whole network, not only from this computer. The list
marks it "Also reachable from your network", so you can tell it was not meant to be private.

**What gets listed.** Only servers that belong to the session: its own processes, plus servers started from the
session's own worktree by a process outside it. Ports below 1024, MCP and debug ports, and anything that does not
answer with an HTML page are left out. The list refreshes while the tab is open and again just after a turn ends.

**The preview window.** Each Open replaces the previous preview window for that session, and opens are handled one
at a time. A preview display started for a native-desktop session is released when you close its window, or once
nobody has watched it for 30 minutes in a row.

**Safety.** Opening a preview is for people only (the assistant and API tokens cannot do it), is recorded in the
audit log, and refuses requests from other websites. The address is built on the server, never from page text.

**Servers in Docker (opt-in).** A `docker compose up` server listens through `docker-proxy` or `rootlesskit`, not a
process of the session, so it is only listed when the session's host sets `VIBERPIT_DEV_SERVER_DOCKER_ENABLED=true`.
ViberPit then asks the Docker engine (`GET /containers/json`, filtered to compose containers, over
`VIBERPIT_DEV_SERVER_DOCKER_SOCKET`, default `/var/run/docker.sock`) for running containers, and lists each
published port of one whose compose working directory is inside the session's worktree. The row is labelled
`docker <container>`. A session that is not in a managed worktree gets no containers. It is off by default because
reading that socket needs the `docker` group, which is as powerful as root; ViberPit only ever lists containers.
It costs one local socket read (a few milliseconds) per scan and starts no program. Rootless Docker and Podman serve
the same API on their own socket (e.g. `$XDG_RUNTIME_DIR/docker.sock`, `$XDG_RUNTIME_DIR/podman/podman.sock`); point
the socket setting there. Only rootful Docker 29 has been checked.

**Not included yet.** No proxied preview, so a server on another host that is bound to loopback is seen through
VNC rather than as a link; no element picker.

Kill switch: `VIBERPIT_DEV_SERVER_DETECT_ENABLED=false` hides the list and stops the scan.
