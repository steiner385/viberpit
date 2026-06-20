# Built-in Account Re-authenticator — Design

**Date:** 2026-06-20
**Status:** Approved (brainstorm) → pending implementation plan

## Problem

When an account's OAuth refresh token dies, the account shows `needs-reauth` and the only fix today is running `claude auth login` in a terminal on the host. The placeholder `POST /api/accounts/{name}/relogin` endpoint just records an audit event — it does not actually re-login. We want a **one-click in-app re-authenticator**. The specific friction the user hits when logging in manually is that their everyday Chrome already has an **active `claude.ai` session as the wrong account**, so the OAuth flow skips the account picker — and the workaround (logging out of Claude) is annoying and risks purging their Google logins.

## Key facts (verified on this host)

- `claude auth login --claudeai [--email]` runs the full OAuth flow; it opens the browser via the **`BROWSER` env var** (fallback `xdg-open`) and completes on a **`127.0.0.1:<port>` localhost callback**. There is no print-URL/no-browser flag. (Binary probe + `--help`, 2026-06-20.)
- Because the callback is localhost-bound, the login browser **must run on the host machine** (the ccswitch host's desktop, `DISPLAY=:0`). A phone/other-device browser cannot complete it. *(User confirmed this model.)*
- `ccswitch.accounts.add_account(name, email)` already runs `claude auth login` in a temp `CLAUDE_CONFIG_DIR` capture dir and stores the captured `.credentials.json` into the account slot (+ merges mcp/claude.json + recomposes). The re-auth reuses this exact capture/store logic.
- Host Chrome: **Google Chrome 149**, binary `google-chrome`. Cookie DB at `~/.config/google-chrome/<Profile>/Cookies` (profiles: `Default`, `Profile 1`, `Profile 2`). `Default` currently has 22 `google.com` cookie rows and 0 Claude/Anthropic rows. Cookie *values* are AES-encrypted via the OS keyring ("Chrome Safe Storage"); the `host_key` column is plaintext, so **deleting** Claude rows needs no decryption.

## Decisions (from brainstorm)

1. **Reuse Claude's real OAuth** by driving `claude auth login` with `BROWSER` intercepted — no OAuth reimplementation.
2. **Login happens on the host desktop** (`DISPLAY=:0`), in a fresh-launched Chrome window.
3. **"Google logged in, Claude logged out"** via the **cookie-copy approach**: copy the user's real profile cookie store into a temp profile, DELETE only the `claude.ai`/`anthropic.com` cookie rows, launch Chrome on the copy. Google carries over (one-click "Continue with Google"); Claude is forced to re-pick; the **live profile is never modified**. Fallback (if Google doesn't carry over): a dedicated persistent login profile with a one-time Google sign-in.

## Architecture

### Components

**1. Fresh-browser launcher — `scripts/ccswitch-reauth-browser` (new, executable)**
A tiny shell script invoked as `BROWSER` by `claude auth login`. It receives the OAuth URL as `$1` and opens it in a Chrome window pinned to a pre-prepared temp profile:
```sh
#!/usr/bin/env bash
# $1 = url ; $CCSWITCH_REAUTH_PROFILE = temp --user-data-dir prepared by the runner
exec google-chrome --user-data-dir="$CCSWITCH_REAUTH_PROFILE" \
     --password-store=gnome-libsecret \
     --no-first-run --no-default-browser-check --new-window "$1"
```
(Binary resolution: prefer `google-chrome`/`google-chrome-stable`, else `chromium`/`chromium-browser`; error if none. The launcher reads the prepared profile dir from `CCSWITCH_REAUTH_PROFILE` so the runner controls it.)

**2. `ccswitch/auth_login.py` (new) — profile prep + login capture**
- `prepare_reauth_profile(source_profile_dir: str) -> str`: create `~/.config/ccswitch/reauth-tmp/<uuid>` (mode 0700); copy `<source>/Cookies` → `<temp>/Default/Cookies` and `~/.config/google-chrome/Local State` → `<temp>/Local State`; run `sqlite3` DELETE of `claude.ai`/`anthropic%` rows on the temp Cookies DB; return the temp dir.
- `cleanup_reauth_profile(temp_dir: str)`: `shutil.rmtree(temp_dir, ignore_errors=True)` (guarded to only remove paths under `reauth-tmp`).
- `capture_login(name, *, env_extra: dict) -> bool`: refactor the existing `add_account` capture/store body into this — run `claude auth login --claudeai` with `env = {**os.environ, **env_extra}` (env_extra carries `BROWSER`, `DISPLAY`, `CCSWITCH_REAUTH_PROFILE`, `CLAUDE_CONFIG_DIR=<capture>`); on a captured `.credentials.json`, store into `slot_path(name)` (+ mcp/claude.json merge + `configdir.compose(name)`); return success. `add_account` now calls `capture_login`.

**3. `server/reauth.py` (new) — `ReauthManager`** (lives on `app.state`)
- Runs **one reauth at a time** (a module/instance lock) as an `asyncio` task.
- `start(name, identity) -> ReauthState`: prepare temp profile → spawn `capture_login` via `asyncio.create_subprocess_exec` (or `to_thread`) with `BROWSER=<launcher>`, `DISPLAY=<settings or ":0">`, `CCSWITCH_REAUTH_PROFILE=<temp>`, source profile from settings. Track state.
- State machine: `idle | running | success | error | timeout` (+ message). **5-minute timeout** → kill the `claude` subprocess + any spawned Chrome (best-effort) → cleanup.
- On success: account status re-derives to `ok` on the next usage poll (or force a `usage_for(name)` refresh); always `cleanup_reauth_profile`.
- `status(name) -> ReauthState`, `cancel(name)` (kill + cleanup).
- Cleanup of the temp profile happens in **every** terminal path (success/error/timeout/cancel).

**4. API — `server/api/accounts.py`** (replace the placeholder `relogin`)
- `POST /api/accounts/{name}/reauth` → validate `name` (exists, `^[A-Za-z0-9._-]+$`), `ReauthManager.start(name, identity)`, 202 with initial state. 409 if a reauth is already running.
- `GET /api/accounts/{name}/reauth` → current `ReauthState`.
- `POST /api/accounts/{name}/reauth/cancel` → cancel.
- All `Depends(current_identity)`; audited (`account.reauth.start|success|error|cancel` with account + identity, never token material).

**5. Frontend** (`web/src/api/hooks.ts` + the account row in `Dashboard.tsx`)
- `useReauthAccount()` (POST start), `useReauthStatus(name, enabled)` (GET, polled ~1.5s while running), `useCancelReauth()`.
- Account row: a **"Re-authenticate"** button (emphasized when `status` is `needs-reauth`/`needs-relogin`). Click → start → inline status: *"Opening a fresh Chrome on the host — sign in there…"* with a **Cancel**; on `success` invalidate `QK.accounts` (status flips off needs-reauth) and show ✓; on `error`/`timeout` show the message. Copy states clearly the window opens **on the host machine's screen**.

### Data flow
```
[Re-authenticate kindash]  → POST /api/accounts/kindash/reauth
  ReauthManager.start:
    prepare_reauth_profile(Default) → /…/ccswitch/reauth-tmp/<uuid> (Google cookies, Claude rows deleted)
    capture_login("kindash", env_extra={BROWSER=launcher, DISPLAY=:0, CCSWITCH_REAUTH_PROFILE=<temp>, CLAUDE_CONFIG_DIR=<capture>})
      → claude auth login → BROWSER launcher → fresh Chrome on host (Google in, Claude out)
      → user picks Google account → localhost callback → .credentials.json captured
      → stored in slot + recompose
  status → success ; cleanup temp profile ; UI: "kindash re-authenticated ✓"
```
UI polls `GET …/reauth` throughout.

## Error handling
- No Chrome binary on host → `error` "no supported browser found on host".
- No `DISPLAY` / cannot open a window → `error` (claude exits non-zero / timeout).
- Source profile / Cookies DB missing → `error` "chrome profile not found: <dir>".
- User closes the window / never finishes → 5-min `timeout` → kill + cleanup.
- `claude auth login` exits without producing `.credentials.json` → `error` "login did not complete".
- Captured account not `max` subscription → succeed but include a warning in the state message.
- A reauth already running → 409 from `start`.
- **Every** terminal path runs `cleanup_reauth_profile`; the temp dir is `0700` and only removed if its path is under `reauth-tmp`.

## Security
- Endpoints `Depends(current_identity)`, tailnet-only, name-validated; audited (no token material in audit or responses).
- The temp profile contains a **copy of the user's Google session cookies** → created `0700` under `~/.config/ccswitch/reauth-tmp/`, never in world-readable `/tmp`, wiped in all exit paths.
- Launches only a benign login page on the host; the user must actively sign in. One reauth at a time.
- Captured tokens stored via the existing encrypted-at-rest path; never returned to the client.
- The launcher script is shipped in-repo (not generated from request input); the runner passes only the prepared profile dir + the URL (from claude) to it.

## Testing
- **Fake `claude` stub** (a script set via `CCSWITCH_CLAUDE_BIN`) that: honors `BROWSER` (invokes it with a dummy URL so the launcher path is exercised in a harness, or is asserted via env), and writes a dummy `.credentials.json` into the capture dir, then exits 0. `capture_login` test asserts it stores into the slot + recomposes — **no real browser, no network, no quota**.
- `prepare_reauth_profile` test against a **fixture cookie DB** (a sqlite file with `cookies(host_key,...)` rows for `google.com` + `claude.ai`): assert the temp profile is created 0700, Google rows survive, Claude rows are gone, `Local State` copied; `cleanup_reauth_profile` removes it and refuses paths outside `reauth-tmp`.
- `ReauthManager` state-machine tests with a fake runner: `start→success`, `start→timeout` (cleanup runs), `cancel` (cleanup runs), 409 when already running.
- API tests (start → status → success, 409, 404 unknown account) with the manager faked.
- Frontend: button + polling + status states (idle/running/success/error) with mocked hooks.
- All existing suites stay green (`pytest server/tests`, `unittest tests`, `web npm test/typecheck/build`).

## Feasibility gate (FIRST task — manual, on the host)
Validate the cookie-copy approach end-to-end: copy `Default/Cookies` + `Local State` to a temp profile, delete Claude rows, launch `google-chrome --user-data-dir=<temp>` at `https://claude.ai`, and confirm **(a)** Google is still logged in (account chooser shows the user's Google accounts without a password prompt) and **(b)** Claude shows logged-out / the login screen. If Google does NOT carry over (keyring decryption fails for the copied DB), switch to the **dedicated-profile fallback** (a persistent `~/.config/ccswitch/login-chrome` profile, one-time Google sign-in, delete Claude cookies before each launch) — the rest of the design is unchanged.

## Security hardening (mandatory-review resolutions)

These are **binding** requirements that amend the sections above.

**Secret-copy lifecycle (the temp profile holds a copy of live Google session cookies + the wrapped cookie key in `Local State`):**
- Create the temp dir with `tempfile.mkdtemp(prefix="reauth-", dir=~/.config/ccswitch/reauth-tmp)` (atomically 0700); pre-create the `reauth-tmp` parent 0700 at server startup. Copy `Cookies`/`Local State` then immediately `chmod 0600`; the inner `Default/` is 0700.
- The **capture dir** (`CLAUDE_CONFIG_DIR`) for the reauth path must also be created with `dir=~/.config/ccswitch/reauth-tmp` — **never the default `/tmp`** (it transiently holds the captured `.credentials.json` and leaks via `/proc/<pid>/environ`). (The legacy CLI `add_account` keeps its current behavior; only the server reauth path changes.)
- `cleanup_reauth_profile(path)` guards with `Path(path).resolve(strict=True).is_relative_to(reauth-tmp.resolve())` (NOT `startswith`; resolves symlinks) and only accepts the path stored in `ReauthState` — never a caller-supplied path.
- **Crash/reboot safety:** cleanup lives in a `try/finally` registered immediately after `prepare_reauth_profile()`. On every server startup, sweep `reauth-tmp/*` dirs older than 10 min and any not in the live `ReauthState`. Install a `SIGTERM`/`SIGINT` handler that runs cancel+cleanup synchronously. The launcher writes its post-`exec` Chrome PID to `<temp>/launcher.pid`; the timeout/cancel kills that PID (Chrome is not always a child of `claude`) so no orphan window holds the cookie copy.
- The **dedicated-profile fallback** (if the feasibility gate fails) is a *different, weaker* posture (a long-lived Google-authed profile on disk). If we ship the fallback, this spec must be re-amended to harden it (its own access controls + Claude-cookie wipe before each launch); we do not silently inherit the cookie-copy hardening for it.

**Input validation (one central helper, applied to ALL `/api/accounts/{name}/*` routes incl. existing refresh/relogin/delete/nickname):**
- `validate_account_name(name)`: regex `^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$`, reject `name in {".", ".."}`, and assert `paths.slot_path(name).resolve().is_relative_to(paths.store_dir().resolve())`. 404 on failure. `start` also 404s if the account slot doesn't exist.

**Process / launcher safety:**
- `DISPLAY` comes from a server-side setting (`Settings.reauth_display`, default `":0"`), read at startup; **no `display` request param**. `CCSWITCH_CLAUDE_BIN`/`claude_bin` from process env / config file only; `subprocess` runs `shell=False` with an argv list.
- The launcher script is installed read-only in the package/install dir (NOT under writable `~/.config/ccswitch/`); the runner `os.path.realpath()`s the `BROWSER=` path and refuses to spawn if it leaves the install dir. Only `$1` (URL from `claude`) and `$CCSWITCH_REAUTH_PROFILE` (from the runner) reach Chrome, as separate argv elements. The launcher validates `$CCSWITCH_REAUTH_PROFILE` is under `~/.config/ccswitch/reauth-tmp/` before `exec`.

**Token handling:**
- `ReauthManager.start` returns **503** if `crypto.key_available()` is False (otherwise the captured token would be stored *plaintext* via the silent fallback — defeating encrypted-at-rest). Message: "configure a master key first (`ccswitch crypto init`)".
- `capture_login` MUST NOT log or audit the captured blob — only a boolean / the key names. Audit rows carry `account` + `identity` only (no `subscriptionType`); the not-Max warning goes in `ReauthState.message`. A `server/tests/test_reauth_no_token_leakage.py` runs the full path with a fake `claude` and asserts no fake-token string appears in audit rows or captured logs.

**Transport / lifecycle:**
- One reauth at a time; `start` returns 409 (with the holding identity + start time) if running. State machine adds a `cancelling` state (cancel is async; the UI must not flip to `success` on a cancelled run). Optional: a 1-per-30s/identity throttle on `start` (defense-in-depth). CSRF: rely on the existing `SameSite=strict` + HttpOnly session cookie — document that relaxing `SameSite` would re-enable CSRF-triggered reauth.

## UX clarity (child-psych transferable notes)
- Keep the "**on the host machine's screen**" wording verbatim and surface the configured `DISPLAY` (e.g. "host desktop `:0`") so a click from a phone/laptop doesn't leave the user staring at a spinner wondering where the window went.
- Make Cancel feel immediate: optimistic UI update on click (don't wait for the ~1.5s poll) so recovery from a wrong-account click is unambiguous.

## Out of scope
- Phone/remote-device login (localhost callback is host-bound).
- Re-login for `--console` (API-billing) accounts; this targets `--claudeai` Max accounts.
- Auto-triggering reauth on `needs-reauth` (user clicks the button).

## Gate outcome (2026-06-20) — feasibility PASSED, two binding corrections
The cookie-copy gate PASSED on the host (real google-chrome 149): Google logged in + Claude logged out. Two corrections (authoritative; supersede illustrative prose above):
1. **Strip query is `DELETE FROM cookies WHERE host_key LIKE '%claude%' OR host_key LIKE '%anthropic%'`** — Claude's cookies are on `claude.com`/`claudeusercontent.com`/`platform.claude.com`, NOT `claude.ai`.
2. **The launcher MUST pass `--password-store=gnome-libsecret`** — otherwise Chrome uses its basic store, cannot decrypt the keyring-encrypted copied Google cookies, and Google appears logged out. (Keyring entry "Chrome Safe Storage" confirmed present.)
