# Reauth profile fidelity — keep Google signed in and 1Password available

**Date:** 2026-09-04
**Status:** approved (chat, 2026-09-04)
**Amends:** `2026-06-20-account-reauthenticator-design.md` (the cookie-copy login flow)

## Problem

Adding or re-authenticating a Claude account opens what looks like a completely fresh Chrome:
no Google account chooser, no 1Password, no saved passwords. Every login is the slow way.

The original design already intended the opposite: `prepare_reauth_profile()` copies the
user's real Chrome `Cookies` + `Local State` into a temp `--user-data-dir` and deletes only
the claude/anthropic rows, so Google stays signed in while Claude shows its login screen.
The 2026-06-20 feasibility gate passed. It was run interactively on the host. In production
the runner lives in `ccswitch.service`, and that is where it silently broke.

## Root cause (proved 2026-09-04, headless probe on dobby)

1. Every session cookie in `~/.config/google-chrome/Default/Cookies` is `v11`-encrypted —
   the key is the gnome-keyring "Chrome Safe Storage" secret. Only 201 low-value `v10`
   cookies are decryptable without the keyring.
2. `ccswitch.service` and `ccswitch-daemon.service` run with **no `DBUS_SESSION_BUS_ADDRESS`
   and no `XDG_RUNTIME_DIR`**. The launcher passes `--password-store=gnome-libsecret`, Chrome
   logs `dbus/bus.cc: Failed to connect to the bus` and silently drops every `v11` cookie.
3. Chrome's stderr never reaches the journal (the `claude` CLI spawns `$BROWSER`), so the
   failure was invisible.

| environment for the reauth Chrome | cookies kept | google.com | Google session cookies with a value |
|---|---|---|---|
| service env as deployed | 201 | 0 | 0 / 5 |
| + `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`, `XDG_RUNTIME_DIR` | 361 | 48 | 5 / 5 |

Claude cookies were correctly stripped in both runs.

1Password is a separate, smaller gap: the copy never included the extension. Only `Cookies`
and `Local State` are copied; `Extensions/`, `Local Extension Settings/`, `Extension State/`,
`Preferences` (where Linux Chrome registers extensions) and `Login Data` are not.

## Decisions

**D1 — Inject the user session bus into the login environment (fixes Google).**
`capture_login()` merges `session_bus_env()` under the caller's `env_extra`: when
`$XDG_RUNTIME_DIR/bus` (default `/run/user/<uid>/bus`) is a socket and the variable is
not already set, add `DBUS_SESSION_BUS_ADDRESS=unix:path=<bus>` and `XDG_RUNTIME_DIR`.
Doing it in `capture_login` (not the server runner) fixes the CLI `ccswitch add` path too and
keeps one source of truth. Explicit `env_extra` values always win. No change to the launcher
script; `--password-store=gnome-libsecret` stays.

**D2 — Copy a curated extension set into the temp profile (fixes 1Password).**
`prepare_reauth_profile(source, *, extension_ids=None)` additionally copies, per configured
extension id, `Extensions/<id>` and `Local Extension Settings/<id>`; plus `Extension State/`
and `Login Data` once; and writes a **minimal** `Default/Preferences` containing only
`extensions.settings[<id>]` for ids that exist in both the source `Preferences` and
`Extensions/`. Default id list: `("aeblfdkhhhdcdjpifhhbdiojplfjncoa",)` (1Password).
Override with `CCSWITCH_REAUTH_EXTENSIONS` (comma-separated; empty string disables).

Probe results that fix the shape of D2:
- Copying the **full** `Preferences` breaks Google again (SID no longer decrypts): it names
  the signed-in Google account whose refresh token lives in the uncopied `Web Data`, and
  Chrome's account reconciler logs the Gaia cookies out. Hence the minimal Preferences.
- With minimal Preferences (with or without `Login Data`), the 1Password service worker
  starts and the Google session cookies decrypt. One transient
  `DidStartWorkerFail <id>: 5` on first boot is normal; the worker is live seconds later.
- `Secure Preferences` on this Linux profile holds no per-key MACs; it is **not** copied.
- `--load-extension` is ignored by branded Chrome ≥ 137, so file copy is the only path.

**D3 — Do not copy `NativeMessagingHosts/com.1password.1password.json`.**
With the manifest the extension defers unlock to the desktop app on `:0`, which is invisible
from the VNC panel a phone user sees. Without it the extension runs standalone and unlocks
with the master password inside the reauth window. Assumption to confirm on the first live
run: the extension falls back to standalone unlock when the app is unreachable; if it instead
shows a "connect to app" error, revisit by making the manifest copy opt-in.

**D4 — Missing pieces degrade to today's behaviour, never fail the login.**
An id with no `Extensions/<id>` dir, an unreadable source `Preferences`, or an absent
`Login Data` is logged at INFO and skipped. Only a missing source `Cookies` raises, as today.

## Security posture

The temp profile already holds live Google session cookies; `Login Data` (saved passwords,
`v11`-encrypted) and 1Password's local state (encrypted vault cache) are the same trust class.
They live under the 0700 temp dir that is removed in the runner's `finally` and by
`sweep_stale()`. No new long-lived state on disk. The bus address is the user's own session
bus; the service already runs as that user.

## Out of scope

- Pinning the 1Password icon to the toolbar (`extensions.pinned_extensions`) — untested.
- Chrome's own Google-account sign-in state (`Web Data` token store) — deliberately absent.
- Any change to the `setup_token` (paste-a-code) path or the Copilot device flow.

## Verification

Unit: `tests/test_reauth_profile.py`, `tests/test_capture_login.py`, new
`tests/test_session_bus_env.py`, `server/tests/test_reauth_manager.py`,
`server/tests/test_config_xvfb.py`. Live: after deploy, add an account from the Accounts page
and confirm the Google account chooser appears without a password and the 1Password unlock
prompt is reachable inside the VNC panel.
