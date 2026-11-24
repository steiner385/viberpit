# Remote Control for ViberPit Claude sessions — plan

**Goal:** every ViberPit Claude coding session also appears in the Claude web / desktop / mobile apps
(Claude Code "Remote Control"), on by default, with a global default and a per-session override.

## Verified mechanism (spike, 2026-11-24, real CLI)

- The CLI's SDK control channel accepts `{"subtype": "remote_control", "enabled": true}` and replies
  `{"session_url": "https://claude.ai/code/session_…", "connect_url": "…", "environment_id": "…"}`
  (bridge tag `remote-control-sdk`). `{"enabled": false}` tears it down and replies `{}`.
- The Python SDK has no public method; send it like the driver's existing private-channel calls
  (`getattr(getattr(self._client, "_query", None), "_send_control_request", None)`, see
  `server/sessions/claude_driver.py:1344`, `:2617`, `:2649`, `:2841`).
- The `remoteControlAtStartup` settings.json key is read only by the interactive REPL — it does NOT
  apply to SDK sessions. Every spawn/resume must send the request itself.
- Inbound prompts typed in claude.ai are enqueued into the CLI's own prompt queue, so they surface
  as CLI-initiated turns; the driver's idle listener (`_turn_active`) already tracks those.

## Semantics

- **Global default** — `AppSettings.remote_control_default` (nullable bool; `None` reads as **on**).
  Exposed in `GET/PATCH /api/settings` as `remoteControlDefault` + `remoteControlCapability`.
- **Per-session override** — `Session.remote_control` (nullable bool; `None` = follow global).
  Effective = `session.remote_control if not None else global default`.
- **Kill switch** — `VIBERPIT_REMOTE_CONTROL_ENABLED` (default `true`; new env vars use the `VIBERPIT_`
  prefix — `ccswitch` spellings fail `scripts/brand_ratchet.py`). Off → never sent, capability false,
  UI hides the controls. Constitution Principle I: undocumented vendor surface stays gated.
- **Eligibility** — Claude engine only, not Copilot accounts (no claude.ai OAuth), not the Assistant
  drawer session (`kind == "assistant"`). Ineligible sessions report `remoteControlAvailable: false`.
- **Last URL** — `Session.remote_control_url` (nullable str), set from the enable reply, cleared on
  disable. Each spawn opens a fresh bridge, so a respawn/rotation overwrites it.
- Enabling is **best-effort and non-blocking on spawn**: failure logs (`session.remote_control_failed`)
  and never fails the spawn or a turn.

## Task 1 — server

1. `server/config.py`: `remote_control_enabled` from `VIBERPIT_REMOTE_CONTROL_ENABLED` (default true),
   mirroring `VIBERPIT_FLAG_SETTINGS_ENABLED`.
2. Models + migration `0113_session_remote_control.py` (chain off the newest revision): nullable
   `Session.remote_control`, `Session.remote_control_url`, `AppSettings.remote_control_default`.
   Update the hand-maintained table lists only if the Postgres tests require it (nullable → no).
3. Driver (`claude_driver.py`): `async def set_remote_control(self, enabled: bool) -> dict` sending the
   control request (timeout ~45 s); returns the reply; raises a clear error when no live client.
4. Orchestrator: `set_remote_control(session_id, enabled, *, identity)` → persists override, applies
   live when a driver exists, stores/clears the URL, returns `{enabled, url}`; `remote_control_available`
   + effective-value helper; after every successful Claude spawn/resume, schedule the enable when
   effective is on (background task, cancellation-safe per `server/aio.py`, logged on failure).
   Plumb through every layer fast mode uses: `facade.py`, `fleet_orchestrator.py`, `remote.py`,
   `server/ipc/orchestrator_rpc.py`, `server/ipc/client.py`/`serve.py` as applicable.
5. API: `POST /api/sessions/{id}/remote-control {enabled}` → `{enabled, url}` with audit
   `session.remote_control`; session payload gains `remoteControl` (effective bool),
   `remoteControlOverride` (bool|null), `remoteControlUrl`, `remoteControlAvailable`.
   Settings: `remoteControlDefault` read/patch + `remoteControlCapability`.
6. Tests (TDD): driver sends the right request (fake `_query`), orchestrator effective/override/
   eligibility/kill-switch, spawn applies when on and not when off/ineligible, API endpoint + payload,
   settings round-trip, migration reconcile. Run all four `lint-and-test` steps.

## Task 2 — web

1. `web/src/api/types.ts` + hooks (`useSetRemoteControl`, settings field).
2. Session rail (`web/src/features/sessions/rail/EnvironmentCard.tsx` area, alongside sandbox
   controls): a "Remote Control" switch (shows inherited vs overridden) and an "Open in Claude app"
   link to `remoteControlUrl` when present. Hidden when `remoteControlAvailable` is false.
3. Settings → Session defaults: "Show sessions in the Claude apps (Remote Control)" toggle, shown only
   when `remoteControlCapability` is true; default on.
4. Tests + full `npm run typecheck && npm run lint && npm run test && npm run build`.

## Caveats documented for the user

- A session appears in the claude.ai account that owns it; rotation moves it to the new account.
- Anyone signed in to that claude.ai account can drive the session from the Claude apps — outside
  ViberPit's Tailscale-identity auth. Approval routing for prompts initiated from the web is to be
  observed after rollout.
