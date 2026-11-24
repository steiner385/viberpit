# Spike: which credential can open Remote Control for ViberPit sessions

**Status:** open, with one decisive experiment pending (§4). **Date:** 2026-11-24.
**Context:** PR #3457 shipped Remote Control (sessions in the Claude web/desktop apps). In
production, every enable is refused.

## 1. Finding: setup-tokens cannot open Remote Control (verified in prod)

After the 2026-11-24 08:44 EST deploy, the dobby daemon logged this for every session:

```
driver.remote_control refused ... code REMOTE_CONTROL_REFUSED
message=Claude.ai login was rejected — run /login, then /remote-control
```

Every session launches with the account's **setup-token** in `CLAUDE_CODE_OAUTH_TOKEN`
(`server/sessions/account_auth.py:oauth_env_for`). Its scope is `user:inference` only, and
`2026-spike-setup-token.md` already records that it "cannot establish Remote Control sessions".

The earlier spike passed only because it ran under the operator's interactive `~/.claude` login.
That login's scopes are `user:inference user:profile user:sessions:claude_code user:mcp_servers
user:plugins user:file_upload user:privacy_consents:write`. Lesson (Principle III): verify a vendor
surface with the **same credential path** production uses, not the developer's login.

## 2. Options

| # | Option | Remote Control | Principle II | Verdict |
|---|--------|----------------|--------------|---------|
| A | Keep setup-tokens; report the refusal honestly | no | ok | **done now** (`fix/remote-control-setup-token`) |
| B | Let sessions use the account dir's browser login (`.credentials.json`, refresh token on disk) | yes | **violates** it: many concurrent CLIs refreshing one rotating token family race, and the refresh token sits on runner-mounted paths | rejected |
| C | Launch sessions with the **broker's `oauth_access` envelope**: a short-lived full-scope access token from the single refresh authority, in `CLAUDE_CODE_OAUTH_TOKEN`, with no refresh token | likely, see §4 | **already compliant**: this is the #2629 design | **recommended** |

## 3. Option C in detail

`src/viberpit/credential_envelope.py` already defines `OAUTH_ACCESS` envelopes:

- The dobby primary daemon is the refresh authority (`accounts.is_refresh_authority`).
- It publishes short-lived access tokens, and runners launch `claude` with them. The CLI then
  "has nothing to rotate" (verified against 2.1.183, per
  `2026-10-02-claude-credential-broker-design.md`).
- **Why it should work:** if those access tokens carry the browser login's scopes (including
  `user:sessions:claude_code`), Remote Control should open.
- **What changes:**
  - Launch selection: prefer an `oauth_access` envelope over the setup-token where one exists, on
    dobby too, not only on broker runners.
  - Account onboarding: an account needs a browser login held by the authority, not only a
    setup-token.

### Open questions

1. **Does the CLI accept Remote Control with an env-passed full-scope access token?** This is the
   decisive experiment, §4.
2. **Lifetime.** Access tokens last hours, and the CLI cannot refresh one it was launched with.
   - What happens to an open bridge, and to inference, when the token expires mid-session?
   - Does the broker design already respawn or re-key long sessions? Read the broker design doc
     § expiry.
   - An expired bridge may need a respawn, or a re-sent `remote_control` request after a token
     swap.
3. **Quota and identity.** Is a bridge opened from an access token tied to the same claude.ai
   account and organization the app shows? Probably yes; check in the app.
4. **Exposure (Principle IV).** A full-scope token can do more than inference. It already lives
   only in process memory under the broker rules. Confirm that's still true on dobby's local path.

## 4. The decisive experiment (not yet run)

The agent session was **not permitted** to run this: it reads the operator's stored login token.
Run it yourself, or grant the permission explicitly.

1. Read `claudeAiOauth.accessToken` from a full browser login into memory. Never print it.
2. Start an SDK client with an **empty** temp `CLAUDE_CONFIG_DIR` and
   `CLAUDE_CODE_OAUTH_TOKEN=<token>`.
3. Send `{"subtype": "remote_control", "enabled": true}`.
4. Read the result:
   - A reply with `session_url` means Option C works; build it.
   - `"Claude.ai login was rejected"` means Remote Control needs an on-disk refresh-capable login.
     Option C is dead, and the feature stays dormant until Anthropic lets inference tokens open
     Remote Control.

Better still: run it with a real broker `oauth_access` envelope on rocky, which is exactly the
production path.

## 5. Interim behaviour (Option A, shipped separately)

- A refused enable records `remoteControlUnavailableReason` on the session and memoizes the refusal
  per account in the daemon, so later spawns skip the doomed request.
- The rail shows the switch disabled with the reason, instead of a misleading "On".
