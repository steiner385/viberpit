# Headless Account Reauth — Design

**Status:** REVISED 2026-06-28 (post-spikes) — primary approach changed to `claude setup-token`.
The host-resident-passkey design below is **superseded** by the revision and kept only as a
documented fallback. See "REVISION: setup-token architecture" immediately below.
**Date:** 2026-06-28
**Feature area:** account auth / reauth (`server/reauth.py`, `src/ccswitch/auth_login.py`, `server/sessions/orchestrator.py`)

---

## REVISION: setup-token architecture (supersedes the passkey approach)

Spike D verified `claude setup-token` directly against the real binary (Claude Code 2.1.195,
real Max account) — finding: `docs/superpowers/specs/spikes/2026-spike-setup-token.md`:

- **1-year** opaque bearer token (`sk-ant-oat01-…`), **non-rotating**.
- Consumed via `CLAUDE_CODE_OAUTH_TOKEN` env var with **no stored `.credentials.json`**.
- **Concurrency-safe**: 6 parallel sessions sharing one token, zero 401s, token still valid after.

This eliminates the root of the diagnosed in-session 401s (single-use refresh-token rotation /
self-collision) because the token does not rotate. It is simpler and more constitution-aligned
(the binary's own sanctioned headless mechanism) than the passkey/CDP scheme, which is therefore
dropped to a fallback.

### Revised architecture

1. **Generation** — per account, run `claude setup-token` (interactive code-paste OAuth, ~yearly)
   through the existing virtual-browser/VNC reauth surface. No loopback port needed.
2. **Storage** — CORRECTED 2026-06-28 (A3 live finding): the systemd `ccswitch-daemon` has **no
   GNOME keyring / D-Bus session** (it gets `CCSWITCH_MASTER_KEY` from
   `EnvironmentFile=/etc/ccswitch/ccswitch.env`, and `secret-tool` fails with no session bus). So
   keyring storage is **unreadable by the daemon** — Phase A's keyring `read_oauth_token` returns
   None in prod (safe fallback, but no token auth). **Corrected storage:** envelope-encrypt the
   token with the master key (`crypto.encrypt`, exactly as `refreshToken` is encrypted in store
   slots — the daemon decrypts via its env master key) and store the ciphertext in a
   daemon-readable file under `paths.store_dir()` (e.g. `oat/<account>`). `read_oauth_token`
   precedence mirrors `master_key()`: env-var override → encrypted file → keyring (interactive
   fallback). Preserves no-plaintext-at-rest; the keyring `oat-<account>` path from Phase A becomes
   the interactive-only fallback.
3. **Injection** — orchestrator sets `CLAUDE_CODE_OAUTH_TOKEN` in each session's SDK subprocess
   env (per-process isolation preserved alongside `CLAUDE_CONFIG_DIR`).
4. **Detection / regen** — a 401 on the token marks the account `needs-reauth` (reusing the
   existing ledger + audit path); the user regenerates via the dashboard ~yearly.

### Kept / dropped

- **Kept:** Phase 1 (in-session-401 force-refresh, already shipped #340), audit instrumentation,
  the keyring/crypto secret path.
- **Dropped:** the host-resident-passkey pipeline, CDP virtual authenticator, `op` password
  fallback (Spike A/B findings retained as a documented fallback only).

### Open design decisions (for approval before planning)

1. **Replace vs. coexist** — DECIDED: **hard replace.** The grant-harvest/refresh path is removed;
   every account requires a setup-token. Risk accepted (big-bang, no fallback). Removes Phase 1's
   in-session-401 refresh (#340) and the Cloudflare-1010 / idempotent-POST hardening as dead code.
   **Mandatory safeguard:** a pre-cutover gate must verify EVERY active account has a working
   `oat-<account>` token (a live auth probe) before the old path is deleted — never cut over into
   a broken state.
2. **Generation surface** — recommend: add a "Generate long-lived token" action to the existing
   add/reauth dashboard flow (reuses the virtual browser), capturing the code-paste step; this
   augments, then replaces, `capture_login`.
3. **Quota/ToS confirmation** — RESOLVED (docs + Spike D): a setup-token "authenticates with your
   Claude subscription… scoped to inference only" — same subscription quota as interactive `claude`,
   no separate metering, Anthropic's sanctioned headless mechanism. Spike D's 6 concurrent calls
   metered as normal inference. ccswitch's usage-aware rotation is unaffected (token changes auth,
   not the quota model). Phase C note: usage *polling* currently uses legacy creds — under hard
   replace it must work via the token (or keep a minimal creds path for usage only).
4. **Revocation path** — RESOLVED (see spike doc Revocation finding): there is **no per-token
   revoke** for a setup-token. It is a 1-year grant; `logout` doesn't touch it; rotation =
   generate-new (old lingers to expiry); only an account-level reset kills it early. Phase B/C
   therefore rely on token *hygiene* (keyring-only, never logged, host-generated) rather than
   revocation, and must inject per-account only (never a process-wide `CLAUDE_CODE_OAUTH_TOKEN`,
   which precedence #5 would apply to every session).

## Problem

Accounts periodically flip to `needs-reauth`, requiring a manual interactive login. The
user wants the system to re-authenticate **programmatically, unattended, from anywhere** —
including when the user is not physically near the host (no Bluetooth proximity to the
server).

### Diagnosis (what is actually failing)

Querying `audit_events WHERE type='account.needs_reauth'` on the live DB
(`~/.claude-accounts/ccswitch.db`) over a ~3-day window:

- **27/27 flips are `path:"live-session"` 401s**, spread across all three accounts
  (~3/account/day). **Zero** are token-refresh-endpoint failures.
- The hop-3 refresh hardening already shipped (Cloudflare-1010 UA fix, idempotent token
  POST, 400/401-vs-transient classification) is holding — the refresh endpoint flips no one.
- The remaining flips happen during a live `claude` turn: an API call returns 401, the
  recovery path recomposes stored creds and relaunches, and a second 401 within 60s flips
  the account to `needs-reauth`.

The in-session 401 recovery (`orchestrator._recover_session`, `_recompose_same_account` →
`_reassign`) **does not force a token refresh before relaunch** — it re-assembles the stored
creds and relaunches the same (possibly stale) token. The live-session flip site also records
`token_expires_at=None`, so we cannot yet prove expired-vs-revoked from the audit rows.

## Goals

1. Re-authenticate accounts **headlessly** (no human in the normal path).
2. Work when the user is **remote** (no device proximity to the host).
3. Keep the **lowest practical blast radius** for any credential that must rest on the host.
4. Fix the cheap, high-value in-session 401 defect found in the diagnosis.

## Non-goals

- Forking/patching the `claude` binary (constitution I). The binary owns the OAuth dance.
- True parallel multi-account reauth (the host has a single reauth Chrome/DISPLAY; see
  Concurrency).
- Phone-prompt / passkey-on-your-phone approval — rejected; see Decision Record.

## Key constraint analysis (why the obvious answers don't work)

There are three authentication hops:

```
[You/Google] --login--> Google account
     └─ (Sign in with Google) --OAuth consent--> Anthropic --issues--> access+refresh tokens
                                                                              └─ ccswitch refreshes (oauth.py)
```

- A **passkey on your phone** cannot approve the server's login remotely. Cross-device
  passkeys use FIDO2 **hybrid transport**, which requires a **BLE proximity check** between
  phone and the device doing the login. This is the anti-phishing core of WebAuthn and cannot
  be configured away. → A phone passkey fails the "remote, no Bluetooth" requirement.
- **Google's "tap yes on your phone" device prompt** *does* work remotely (internet-relayed,
  no Bluetooth) — but it is a human tap, not headless.
- The constraints form a **trilemma — pick two**:

  | Want | Cost |
  |---|---|
  | Headless + Remote | a high-value credential must live **on the host** |
  | Headless + No-stored-cred | survive purely on refresh-token longevity; no re-login at all |
  | Remote + No-stored-cred | not headless — a remote Google device-prompt tap per gate |

**Decision: Headless + Remote**, accepting a host-resident credential — but the
*lowest-blast-radius* one available.

## Chosen approach: host-resident passkey + transient 1Password password fallback

The "passkey that works with no Bluetooth" is solved by putting the passkey **on the server**.
A host-resident **software/virtual FIDO2 authenticator** signs Google's WebAuthn challenge
locally — no phone, no BLE, no proximity, no gesture, no human. It is a passkey, just resident
on the host.

Why a host passkey beats a stored password as the primary credential:

- **Origin-scoped** to `google.com` — useless if exfiltrated to any other site.
- **No account-recovery power** (unlike a password).
- **Independently revocable** — delete that one passkey from the Google account.

### Escalation ladder (each rung is headless + remote unless noted)

1. **Force token refresh** (the diagnosis fix): on the *first* in-session 401, call
   `ensure_valid_token` before recompose/relaunch. Cheapest possible recovery.
2. **Re-grant via host passkey**: drive `claude auth login --claudeai` against a persistent,
   per-account Chrome profile on the virtual display; inject the per-account virtual
   authenticator + credential via CDP; Google's passkey challenge is satisfied locally;
   `capture_login` harvests fresh tokens.
3. **Password fallback** (rare residual): if Google escalates past the passkey to a typed
   password, fetch it **transiently** from 1Password via `op` and type it. Requires the
   account's *second* factor to also be host-satisfiable (the host passkey) — see Secrets.
4. **Terminal**: only if all rungs fail → `_mark_needs_reauth` (today's *first* response
   becomes the *last*), park the account, notify.

## Components

All additive; reuse existing machinery (`capture_login`, `oauth.py`, the chromium
shim + virtual X, `push.py`, `ledger`, `crypto.py` keyring/encryption).

1. **`ProfileStore`** — owns one **persistent** Chrome profile per account under a new
   `paths.reauth_profiles_dir()/<account>` (mode 0700). Distinct from the existing *ephemeral*
   `reauth-tmp`. Strips only Claude/Anthropic cookies before a re-grant; the profile is the
   thing kept warm. **Replaces** the existing single shared `settings.chrome_profile`
   assumption for the auto-reauth flow (no parallel half-dead path).

2. **`PasskeyVault`** — per-account host passkey private key, `google.com`-scoped, stored
   **encrypted at rest** via existing `crypto.encrypt` (master key from the GNOME keyring).
   Injected into the login browser via CDP `WebAuthn.addVirtualAuthenticator` +
   `WebAuthn.addCredential` (`automaticPresenceSimulation=true`, `isUserVerified=true`).

3. **`GrantRunner`** — wraps `capture_login` parameterized with a `ProfileStore` profile + a
   virtual display + a CDP attach that injects the passkey before the WebAuthn challenge.
   Returns a structured outcome (`granted` / `needs_password` / `needs_human` / `failed`)
   instead of a bare bool. **Runs to completion** (awaits a terminal state) — it does NOT use
   the fire-and-forget `ReauthManager.start()`.

4. **`LoginObserver`** — passive CDP read of page state to classify the terminal condition
   (granted / password-demand / other-escalation / stuck-timeout). Read-only; worst case is a
   misclassification that falls through to the timeout path. On `needs_password`, triggers the
   `op` fetch; on other escalation, triggers `push.notify` + park.

5. **`ReauthCoordinator`** — the brain. Single-consumer **queue** that serializes all
   re-grants, dedupes per account, applies cooldown/backoff, and is the single source of truth
   for "is this account being re-granted right now." Subscribes to both triggers (below).

## Triggers / data flow

**Proactive (heartbeat — keeps the reactive path from firing):** extend the existing
`_token_refresh_monitor` / `_refresh_idle_tokens_once`. Per account, in series: if the token
is near expiry or the profile warmth check fails → `ReauthCoordinator.enqueue(account,
reason="proactive")`.

**Reactive (safety net — the diagnosis case):** the in-session 401 path gains rungs:

```
live-turn 401 → _recover_session(kind="auth_401")
  1st 401  → ensure_valid_token (FORCE REFRESH)  ← diagnosis fix (rung 1)
             then recompose + relaunch
  still 401 / grant dead → ReauthCoordinator.enqueue(account, reason="reactive-401")
  re-grant exhausted     → _mark_needs_reauth (terminal, rung 4)
```

`_mark_needs_reauth` is demoted from first response to last resort.

## Concurrency & three-account validation

Validated by tracing three simultaneous re-grants through the real lock (`reauth.py`):

- The existing `ReauthManager._lock` is **fail-fast, not a queue**: `start()` holds the lock
  only to flip state to `running` and spawn `_run()` as a background task, then returns; a
  second `start()` while running **raises `ReauthBusy`**.
- **Serialization therefore comes from the `ReauthCoordinator`'s own single-consumer queue**,
  not the lock. Because asyncio is single-threaded and the consumer never awaits between
  "check dedup set" and "mark in-flight," three accounts serialize with **no races and no
  deadlock**.
- Common case (passkey, headless): three re-grants drain in series, each a few seconds,
  ~under a minute total, on the single Chrome/DISPLAY (`REAUTH_DISPLAY_KEY`). Correct.
- **Caveat 1 — user preemption (must-fix):** a dashboard add/reauth also calls `start()`; if a
  background run holds `running`, the manual login gets `ReauthBusy` 409. The add/reauth entry
  point must `cancel()` an in-flight *background* run first, then start; the Coordinator treats
  that cancellation as "re-enqueue, don't error."
- **Caveat 2 — head-of-line on escalation:** if a re-grant stalls awaiting a non-headless
  escalation it occupies the only browser. With the host passkey + password fallback the
  normal flow has **no human step**, so this is now rare; bound it with a short
  browser-occupancy timeout (~2–3 min) → cancel, park, move to next queue item.

## Secrets model

Routes through the **existing** GNOME keyring / `crypto.py` integration (`secret-tool`,
`store_key_in_keyring`, envelope encryption) — no new secrets subsystem.

| Secret | Storage | Used when |
|---|---|---|
| `master-key` (exists) | GNOME keyring via `secret-tool` | Envelope-encrypts store creds |
| **`op` service-account token** (new) | GNOME keyring, same `secret-tool` pattern | Authenticate `op` headlessly at escalation |
| **Host passkey private key** per account (new) | Encrypted at rest via `crypto.encrypt`, `google.com`-scoped | Injected via CDP on every re-grant (primary path) |
| **Google password** | **Not on the host** — only in 1Password | Fetched transiently via `op read "op://<vault>/<acct>/password"` only on a password escalation; typed, never persisted |

Notes:
- `op` 2.34.0 is installed on the host; headless use requires `OP_SERVICE_ACCOUNT_TOKEN`
  (1Password **service account**) scoped to a vault holding one item per Google account.
- The password fallback only completes headlessly if the factor Google demands **after** the
  password is also host-satisfiable — i.e. the account's registered 2FA must be the **host
  passkey**, not SMS/authenticator-code. Avoid SMS-only 2FA. (Optional future rung: store a
  TOTP seed for a forced-code path.)

## Error handling

- **Keyring locked after host reboot:** the GNOME keyring is unlocked per session/login
  context; after a reboot it may be locked, making the `op` token and master key unreadable
  until unlocked. The chain stalls (accounts park) rather than failing unsafely. Mitigation:
  unlock-at-boot strategy or the `CCSWITCH_MASTER_KEY` env fallback; surfaced as a health
  signal. **Must be designed in the plan.**
- **CDP attach fails / passkey rejected:** fall to password fallback, then terminal park.
- **`op` fetch fails (token expired/revoked):** skip password rung → terminal park + notify.
- **Observer misclassification / stuck:** hard timeout → cancel → park.
- All credential material (password, keys) is never logged (extends the existing
  `capture_login` "blob is never logged" discipline + the audit redaction layer).

## Verification spikes (Principle III — before implementation)

1. **CDP attach to the login browser:** confirm we can launch the `claude auth login` Chrome
   with a remote-debugging port (via the `BROWSER` launcher env) and inject a virtual
   authenticator + credential before the WebAuthn challenge.
2. **Google accepts a virtual-authenticator passkey from the server IP** without escalating —
   the dominant external risk. Determines how often the password rung fires.
3. **Keyring unlock-after-reboot** behavior on the host, and the chosen unlock strategy.

## Decision record

- Headless reauth via **phone passkey** — **rejected**: cross-device passkey needs BLE
  proximity; fails the remote requirement.
- **Warm-cookie + Google device-prompt** (remote tap, no stored cred) — **superseded**: user
  chose fully unattended over no-stored-credential.
- **Host-resident passkey + transient 1Password password fallback** — **chosen**.
- **Sequential re-grants reusing the existing lock** (not true parallelism) — **chosen**.

## Open questions for plan phase

- Exact unlock-at-boot mechanism for the keyring.
- Whether to keep the Anthropic/claude.ai session cookie warm too (cheapest renewal path,
  vs. today's `prepare_reauth_profile` stripping it).
- Whether to add a stored TOTP seed as a third fallback rung.
