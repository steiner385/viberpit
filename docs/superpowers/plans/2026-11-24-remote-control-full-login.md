# Remote Control via the account's full claude.ai login — plan

**Goal:** a ViberPit Claude session can be driven from ViberPit and the Claude apps at once. The
verified recipe is in `docs/superpowers/specs/spikes/2026-11-24-remote-control-auth-spike.md`:
launch with the account's **full-scope slot access token** in `CLAUDE_CODE_OAUTH_TOKEN` plus
`CLAUDE_CODE_ORGANIZATION_UUID`, never pass a refresh token, and rotate the token live with a
top-level `{"type":"update_environment_variables","variables":{"CLAUDE_CODE_OAUTH_TOKEN":…}}`
stdin message.

**Builds on:** PR #3462: `remote_control_unavailable_reason`, the per-account refusal memo, and the
`REMOTE_CONTROL_*` codes in `server/sessions/remote_control.py`.

**Scope:** primary-host (dobby) sessions. Sessions on broker runners (`runner_uses_envelope`) at
first kept their old behaviour and reported the unavailable reason `runner`.

**Follow-up, done (Remote Control on runners):** runners now follow the primary's login. What
was built:

- **Minting.** `claude_authority.mint_remote_control_envelopes` vends a second, Remote-Control-only
  `oauth_access` envelope per ready account: the slot's access token plus the org UUID. It has
  its own generations. It goes on the `creds_sync` push as `rc_envelopes`, beside the inference
  envelope.
- **Runner storage.** The runner keeps them in `credential_envelope.rc_runner_store()`: the
  same lease, and its own 0600 cache.
- **Launch.** `account_auth.remote_control_env_for` / `remote_control_readiness` /
  `slot_access_token` read that store on a runner. So the launch, the toggle and `login_needed`
  all work unchanged. A runner launch is stamped `slot`.
- **Upkeep.** Each sync runs the authority's upkeep on the runner
  (`remote_control_credentials_synced` → `_maintain_remote_control_logins`): live token
  pushes, pending revocations, and lifting `login_needed`.
- **Ownership.** `sessions.launch_credential` (revision 0114) makes a runner's launch kind
  visible to the primary's mirror. `runner` now means only a worker outside the broker.

## Principle II rules this plan must keep

- Only the primary refresh authority refreshes a slot (`accounts.is_refresh_authority`). The CLI
  never receives a refresh token through env or argv.
- The access token reaches the CLI in `CLAUDE_CODE_OAUTH_TOKEN`. This is the existing envelope
  precedent. FD delivery (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`) needs a custom SDK transport;
  track it as a follow-up issue.
- New code never logs or returns a token value.
- Fail closed: no valid slot means fall back to the setup-token, and Remote Control is
  unavailable with the reason `login_needed`.

## Task A: keep slots alive, and let an operator re-run the browser login

Touches `src/viberpit/accounts.py`, `server/sessions/orchestrator.py` (idle sweep),
`server/api/accounts.py`, `server/sessions/reauth.py`, and `web/src/features/accounts/`.

1. **Refresh even with live sessions.** The idle-token sweep (`_refresh_idle_tokens_once`,
   orchestrator.py ~4113) may refresh a slot even when the account has live sessions, as long as
   none of them owns the slot's token family. Every session launched with an env token (a
   setup-token or a slot access token) qualifies; that is all sessions on this path. Keep the
   `has_live_session` exemption only for a session that really launched from the cfg-dir
   `claudeAiOauth`. Add an explicit predicate for this and test it.
2. **Surface a dead grant.** A slot whose refresh fails with `AuthError`, or whose
   `refreshTokenExpiresAt` has passed, reports `login_needed` for Remote Control. It must not be
   hidden by `_setup_token_usage_fallback`. Keep inference on the setup-token unchanged.
3. **Re-run the browser login for an existing account.** Add a person-only route,
   `POST /api/accounts/{name}/browser-login`.
   - It reuses `ReauthManager` with `mode="login"` (`capture_login`) and is allowed even when a
     slot exists. On success it calls `mark_reauthenticated`.
   - Web: on the account row, a "Sign in to claude.ai (Remote Control)" action that reuses
     `ReauthBrowserPanel`.
4. **Slot status helper.** `slot_status(name) -> ok | login_needed | missing`. It reads
   metadata only: expiry, `refreshTokenExpiresAt`, and the `sessions:claude_code` scope.

## Task B: launch with the slot token, and rotate it live

Touches `server/sessions/account_auth.py`, `server/sessions/orchestrator.py` (`_oat_env`,
remote-control helpers), `server/sessions/claude_driver.py`, and `server/sessions/remote_control.py`.

1. **Launch env.** Add `remote_control_env_for(account) -> dict | None` in `account_auth.py`.
   - Primary only, not Copilot.
   - Calls `accounts.ensure_valid_token`, which refreshes as the authority.
   - Returns `{"CLAUDE_CODE_OAUTH_TOKEN": slot_access, "CLAUDE_CODE_ORGANIZATION_UUID": org}`, or
     None when there is no valid full-scope slot or no org. Read the org from
     `paths.store_claude_json_path(name)` → `oauthAccount.organizationUuid` with a small helper.
2. **Choose per spawn.** `_oat_env(session_id)` uses `remote_control_env_for` when the session's
   effective Remote Control is on and the session is eligible. Otherwise it uses `oauth_env_for`,
   as today.
   - Record on the session which credential it launched with: `rc_credential = "slot" | "setup"`.
   - A session launched on the setup-token gets the reason `login_needed` instead of a doomed
     enable.
   - `_session_uses_setup_token` and `_stored_credentials_valid` must follow the session's actual
     launch credential.
3. **Live rotation.** Add a driver method `push_oauth_token(token)`.
   - It writes the top-level `update_environment_variables` message through the SDK transport
     (`self._client._transport.write`), serialised with a lock and guarded when no client is live.
   - The orchestrator pushes the fresh access token to every live `slot` session on that account
     whenever the authority refreshes the slot (hook into `ensure_valid_token`'s success, or the
     sweep), and well before expiry. The sweep must refresh slots used by live `slot` sessions
     ahead of `expiry_skew_sec`.
4. **Toggle-on mid-session** for a session launched on the setup-token: push the slot token first,
   then send `remote_control`. If there's no valid slot, return 409 `REMOTE_CONTROL_REFUSED` with
   the `login_needed` message.
5. **Clear the refusal memo.** `mark_reauthenticated`, or a successful browser login, clears that
   account's `login_rejected` memo, so no daemon restart is needed.

## Task C: show prompts typed in the Claude apps in ViberPit's transcript

Touches `server/sessions/translator.py` and `server/sessions/claude_driver.py`.

- Today `_prompt_echo` swallows ViberPit's own echoed prompt and drops every other top-level plain
  user text. Instead, a top-level plain-text user message that is not an echo of a ViberPit prompt
  should emit `ev.UserMessage` with its `uuid` as `src_uuid` and `origin="remote"` (or the closest
  existing field), so it renders as a user bubble.
- Guard against duplicates: keep a bounded record of prompt texts and uuids ViberPit sent (queued
  and steer prompts included). Never emit for those.
- Tests: own prompt → no duplicate; queued second prompt → no duplicate; steer echo → no
  duplicate; foreign prompt → one bubble; tool_result → unchanged.

## Verification (each task)

- TDD.
- Focused tests plus `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`.
- `pytest tests/` when `src/` is touched, with `PYTHONPATH=<checkout>/src` (the shared venv imports
  another checkout).
- Web: typecheck, lint, the full vitest run, build, budget.
- Do not run the full server suite locally (memory guard); CI runs it.
- After merge and deploy, a live check:
  1. The operator re-runs the browser login for one account.
  2. Start a session and confirm it shows up in claude.ai.
  3. Send a prompt from claude.ai and confirm it appears in ViberPit.
  4. Approve the tool card in ViberPit.
