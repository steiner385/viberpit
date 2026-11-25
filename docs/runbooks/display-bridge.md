# Display bridge: one host's sessions open GUI windows on another host's desktop

Spec: `docs/superpowers/specs/2026-11-25-per-host-display-target-design.md`. Code:
`server/sessions/display_bridge.py` (target-side bridge), `display_bridge_broker.py` (edge),
`display_bridge_env.py` (source-side env and fallback reasons).

A fleet host (the **source**, e.g. `rocky`) can show the windows its sessions open (`foot`, `xeyes`,
headed Chromium) on another host's logged-in desktop (the **target**, e.g. `dobby`). The target
daemon runs `waypipe client` and an SSH connection to the source that forwards the Wayland socket and
(optionally) an X11 socket. One target per source host.

## 1. Install

On **both** the target and the source:

```bash
sudo apt install waypipe        # 0.11 verified 2026-11-25
```

The source also needs libxcb >= 1.13 (the X11 `DISPLAY` is a socket path, which older libxcb ignores),
and `xauth`.

## 2. SSH trust (target -> source)

The **target daemon's Unix user** must reach the **source host as the same Unix user that runs
sessions on the source**, with a key, non-interactively (`BatchMode`; no password or host-key prompt
can be answered).

- Destination: the source Host's `display_ssh_host` if set, else its `ssh_host` (Settings -> Fleet, or
  the Host row).
- Test from the target, as the daemon user: `ssh -o BatchMode=yes <dest> true`.
- If it connects as a different user than the source's session user, sessions fall back with
  `bridge_wrong_user`.

## 3. Accept the source on the target

On the **target's** daemon env (`/etc/viberpit/viberpit.env`):

| Variable | Default | Meaning |
|---|---|---|
| `VIBERPIT_DISPLAY_BRIDGE_ACCEPT_FROM` | empty (accept nobody) | Comma-separated source host names allowed to draw on this desktop, e.g. `rocky` |
| `VIBERPIT_DISPLAY_BRIDGE_ENABLED` | `true` | Kill switch. `false`: every host behaves as local; no fallback is recorded |
| `VIBERPIT_DISPLAY_BRIDGE_X11` | `true` | `false` drops the X11 leg (Wayland only) |
| `VIBERPIT_DISPLAY_BRIDGE_START_TIMEOUT_S` | `15` | How long a spawn waits for the bridge to come up |
| `VIBERPIT_DISPLAY_BRIDGE_GRACE_S` | `60` | How long the bridge stays up after the last session that needs it ends |

Env edits need a **daemon restart**, which touches every live session on that host: it drains
in-flight turns first and blocks, which is expected. Do it when the Sessions page shows nothing
mid-turn (see `deploy/README.md` "Stopping / restarting the daemon").

## 4. Choose the target

Settings -> Fleet -> the source host's **Display** select -> "`<target>`'s desktop". "Local" (the
default) keeps the source's own display behaviour.

## 5. Troubleshooting

Fleet settings shows a chip under the select:

| Chip | Meaning | Fix |
|---|---|---|
| `-> dobby: bridge up` | Bridge is running for this source | Nothing |
| `-> dobby: desktop logged out` | The target has no logged-in graphical session | Log in at the target's console/desktop; the next session goes remote |
| `-> dobby: not accepted` | Target's `ACCEPT_FROM` does not list this source | Add the source name, restart the target daemon |
| `-> dobby: unavailable` | Target not probed/reachable, or the bridge feature is off there | Check the target daemon is up and the edge can reach it; check `VIBERPIT_DISPLAY_BRIDGE_ENABLED` |

A session that could not go remote runs on a virtual display and records a fallback reason:

| Reason | Cause |
|---|---|
| `bridge_not_accepted` | Same as the chip `not accepted` |
| `target_desktop_unavailable` | Same as `desktop logged out` |
| `bridge_unavailable` | The bridge did not come up within the start timeout: SSH failed, waypipe missing, or prep failed (see exit codes) |
| `bridge_wrong_user` | The forwarded socket exists but belongs to a different Unix user than the session user: fix the SSH user (section 2) |

Bridge setup logs (INFO, target daemon: `journalctl -u viberpit-daemon.service | grep -i bridge`) name
the prep script's exit code:

| Exit | Meaning |
|---|---|
| 4 | The private dir `$XDG_RUNTIME_DIR/viberpit-bridge` on the source is not owned by the SSH user, or is a symlink |
| 5 | The source login has no usable `$XDG_RUNTIME_DIR` (unset, relative, missing or not its own). Make sure the SSH login gets a systemd user runtime dir (`loginctl enable-linger <user>`) |

Source-side layout (fixed, one target per source): Wayland socket `$XDG_RUNTIME_DIR/wayland-viberpit-bridge`;
X11 socket `$XDG_RUNTIME_DIR/viberpit-bridge/X11` (`DISPLAY` is that path); cookie
`$XDG_RUNTIME_DIR/viberpit-bridge/xauth` (0600, in a 0700 dir). The cookie and SSH keys never appear
in argv, logs, RPC or API responses.

## 6. Security

While a bridge is up, **X11 programs on the source can read the target desktop's input and screen**;
Wayland programs cannot. Set `VIBERPIT_DISPLAY_BRIDGE_X11=false` on the target if that is not
acceptable. The bridge exists only while a remote (or queued/held) session needs it, closes after the
grace period, and fails closed (treats its set as empty) after about 90 s without hearing from the edge.

## 7. Known limits

- A host added to the fleet after the edge started is not bridged until the edge restarts (same limit
  as dispatching sessions to it).
- After changing a source's SSH destination, the X11 cookie stays at the old login until that source's
  bridge ends (wait out the grace, or end its sessions).
- Remote browser windows need the source's native display feature enabled.
