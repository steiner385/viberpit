---
slug: remote-desktop-vnc-x11
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
  prs: [130, 331, 332, 427, 428, 399, 341, 204, 339, 373]
---

# Remote Desktop (VNC / X11 Browser Bridge)

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
its `DISPLAY` is the host's real X display (`CCSWITCH_NATIVE_DISPLAY`, default `:0`) and
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

**Requirements / limits.** The daemon runs as the desktop user and the X server must
accept `SI:localuser:<user>` (GDM's default; otherwise `xhost +SI:localuser:<user>` in the
session startup). No `XAUTHORITY` is set. `CCSWITCH_NATIVE_DISPLAY` must not fall inside
the Xvfb pool range or native routing disables itself with a warning (the orphan sweep
reaps that range). Behind `tailscale serve` (no `X-Real-IP`) locality resolves to unknown →
virtual. Native routing is effectively **Claude-only** today: only
`ClaudeAgentDriverFactory` is built from the orchestrator's `_session_factory`, the one seam
that injects `DISPLAY`, so a Codex session resolved native takes no Xvfb lease *and* gets no
`DISPLAY` — pass `display: "virtual"` (or use the kill switch) for Codex sessions launched
from a runner's own desktop. In fleet mode an explicit `display: "native"` from a *remote*
requester skips the locality proof but not placement, so under auto placement its windows
land on whichever desktop-capable host ranks first, not necessarily the one the requester
expected — pin `host` to be sure. Kill switch: `CCSWITCH_NATIVE_DISPLAY_ENABLED=false`.
