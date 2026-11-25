---
slug: display-bridge
parent: fleet-and-delivery-operations
title: Per-host display target
state: live
links:
  code:
    - server/sessions/display_bridge.py
    - server/sessions/display_bridge_broker.py
    - server/sessions/display_bridge_env.py
    - server/ipc/display_bridge_rpc.py
---

# Per-host display target

A fleet host's sessions can open GUI windows (terminals, `xeyes`, headed Chromium) on another fleet
host's logged-in desktop, over a waypipe + SSH bridge the target daemon runs while a session needs it.
Chosen per source host in Settings -> Fleet -> Display. Sessions fall back to a virtual display, with a
recorded reason, when the bridge is not accepted, down, or the target desktop is logged out.

Operator setup, troubleshooting and the X11 security note: `docs/runbooks/display-bridge.md`.
