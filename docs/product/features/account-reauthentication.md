---
slug: account-reauthentication
title: Account Re-Authentication
state: live
links:
  code:
    - server/reauth.py
    - server/reauth_remote.py
    - server/ipc/reauth_rpc.py
    - src/ccswitch/setup_token_oauth.py
    - src/ccswitch/auth_login.py
    - server/mcp_auth.py
    - server/sessions/account_auth.py
    - server/usage/auth_failure_audit.py
  prs: ['#354', '#357', '#366', '#358', '#387', '#877', '#331', '#732']
---

# Account Re-Authentication

**Problem** — OAuth refresh tokens expire or get revoked, and accounts log out. When that happens an
account silently starts failing usage fetches and session launches, and the user has no headless way
to re-authenticate a daemon-hosted account (the daemon has no browser and no keyring). MCP servers
add a second auth surface that must be re-established per account.

**Target users** — Anyone whose registered account has flipped to needs-reauth: web-dashboard users
re-authing a specific account row, operators of the headless session daemon, and users of
MCP-backed tools who need `mcpOAuth` refreshed across accounts.

**Desired outcome** — A one-at-a-time, observable re-auth flow that recovers an expired/logged-out
account without shell access: the user starts re-auth on an account, receives an authorize URL,
pastes back the code (headless paste-back OAuth) or drives a browser on a virtual X display viewable
over web VNC, and the account returns to healthy. A successful re-auth clears the sticky
dead-account / needs-reauth caches so rotation immediately picks the account up again. MCP auth
follows the same paste-back shape across accounts.

**Success metrics** — (unknown — needs owner input). Grounded candidates: fraction of needs-reauth
accounts recovered without CLI/shell access; time from needs-reauth flip to recovery; zero token
material leaked in the process (enforced by `test_reauth_no_token_leakage.py` and the
"NEVER log the token" contracts in `setup_token_oauth.py`/`account_auth.py`).

**Non-goals** — Automatic re-auth without a human (OAuth requires a user-driven authorize step).
Running re-auth on a worker — it must run on the master and stream the display from there
(#732/#733). Bulk/parallel re-auth — the ReauthManager is deliberately one-at-a-time.

**Notes** —
- `ReauthManager` (`server/reauth.py`) is a state machine (idle → running → success/error/timeout,
  plus `awaiting_code`/`submit_code` for the setup-token path) with a startup stale-sweep and
  cancel. `reauth_remote.py` + `server/ipc/reauth_rpc.py` project it across the daemon RPC boundary.
- Setup-token generation moved from headless PTY scraping of `claude setup-token` to an in-process
  OAuth2 PKCE flow (`setup_token_oauth.py`, #366): the user pastes the code, ccswitch exchanges it
  for a ~1-year `sk-ant-oat01-…` setup-token, stored encrypted. `account_auth.py` turns that stored
  token into `CLAUDE_CODE_OAUTH_TOKEN` for every launch and helper call site — so an account with a
  dead browser-OAuth refresh token but a live setup-token authenticates everywhere.
- Setup-token SCOPE decides whether the recovered account can report quota. `user:inference` alone
  launches sessions but is refused by `GET /api/oauth/usage` — with a 429, not a 401 — so an account
  recovered this way showed no usage at all and sat in a permanent usage-fetch backoff. `begin()`
  now requests `user:inference user:profile` (`DEFAULT_SCOPE`); every working credential slot carries
  `user:profile` and reads usage 200, while narrow-scope setup-tokens get 429 from the same host
  seconds apart. A code is bound to the scope it was consented to, so a refusal cannot be retried at
  exchange time: `_retry_at_fallback_scope` re-issues the challenge once at `FALLBACK_SCOPE`
  (`user:inference`), costing one extra paste but never leaving re-auth worse than before.
  `CCSWITCH_SETUP_TOKEN_SCOPE` pins the scope without a deploy — the only recovery for a refusal on
  the *authorize* page, where no code is ever produced. A token already minted at the narrow scope
  cannot gain the scope retroactively, so its account reports quota `UNSUPPORTED` (a permanent
  capability gap, not a fetch failure) until it is re-minted.
- Browser re-auth alternative (#331): the reauth browser runs on a virtual X display viewable via
  web VNC; `auth_login.py` prepares a temp Chrome profile with Claude cookies stripped and cleans it
  up on cancel/timeout/error (#745).
- Failure detection: a `set_auth_failure_sink` hook emits `AuthFailure` on each needs-reauth flip;
  `server/usage/auth_failure_audit.py` records these. A 400 refresh response is treated as a dead
  grant and short-circuits future usage calls until `mark_reauthenticated` clears it (#680); a
  Cloudflare 403 is NOT treated as a dead grant (#195, #240) to avoid mass-flipping every account.
- MCP auth (`server/mcp_auth.py`, #877): `claude mcp login` in a PTY prints an authorize URL, blocks
  on the pasted redirect URL, then merges the new `mcpOAuth` into the shared store and clears stale
  per-account caches — the same headless paste-back shape, across accounts.

**Evidence** — State machine in `server/reauth.py`; RPC projection in `server/reauth_remote.py` and
`server/ipc/reauth_rpc.py`; PKCE setup-token flow in `src/ccswitch/setup_token_oauth.py`; browser
profile prep/cleanup in `src/ccswitch/auth_login.py`; token→env in
`server/sessions/account_auth.py`; MCP paste-back in `server/mcp_auth.py`; failure audit in
`server/usage/auth_failure_audit.py`. PRs: #354/#357/#355/#358 (setup-token reauth phases), #366
(in-process PKCE), #387 (reauth button on all accounts), #877 (MCP auth across accounts, headless
paste-back), #331/#332 (VNC-viewable browser reauth), #732/#733 (run/stream reauth from the master).
