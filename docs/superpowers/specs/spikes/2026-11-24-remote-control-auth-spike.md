# Spike: making Remote Control work for ViberPit sessions

**Status:** answered. A working recipe has been verified end to end against claude 2.1.331.
**Date:** 2026-11-24.
**Goal:** drive the same session from ViberPit and from the Claude web/desktop apps at once, like
Claude Code Remote Control.

## 1. Why production is refused

PR #3457 shipped the `remote_control` SDK request. Every production enable was refused:

```
REMOTE_CONTROL_REFUSED  Claude.ai login was rejected — run /login, then /remote-control
```

- Sessions launch with the account's **setup-token** in `CLAUDE_CODE_OAUTH_TOKEN`. Its scope is
  `user:inference` only.
- The CLI raises that message (`Mi()` → `v2_session_create_oauth_rejected`) when the **claude.ai
  server** rejects the token on session creation. It is a scope check on the server, not a
  client-side "must be an interactive login" rule.
- The first spike passed only because it ran under the operator's interactive `~/.claude` login.
  Principle III: test a vendor surface through the credential path production actually uses.

## 2. Experiments

All runs used the system `claude` 2.1.331, which ViberPit runs through
`claude_bin.resolve_claude_bin()`. The bundled 2.1.183 has none of the host-credential surface.
Each run had an **empty** temp `CLAUDE_CONFIG_DIR`, so there was no on-disk login and no refresh
token. The token was the operator's full-scope claude.ai access token, held in memory only.

| # | Launch environment | Result |
|---|--------------------|--------|
| E1 | `CLAUDE_CODE_OAUTH_TOKEN=<full-scope access token>` | refused `remote_control_auth` |
| E2 | host-refresh mode: `CLAUDE_CODE_ENTRYPOINT=local-agent` + `SDK_HAS_OAUTH_REFRESH` + `SDK_HAS_SESSIONS_OAUTH_REFRESH`, no token | refused: "requires a claude.ai subscription"; the CLI never asked the host |
| E2b | E2 + `CLAUDE_CODE_OAUTH_TOKEN` | refused `remote_control_auth` |
| **E3** | E1 + `CLAUDE_CODE_ORGANIZATION_UUID` + `SUBSCRIPTION_TYPE` + `RATE_LIMIT_TIER` | **works**: `session_url` returned |
| **E3o** | E1 + `CLAUDE_CODE_ORGANIZATION_UUID` only | **works**: this is the minimum |
| E3s | E1 + `CLAUDE_CODE_SUBSCRIPTION_TYPE` only | refused |
| **E4** | E3o, then a top-level `{"type":"update_environment_variables","variables":{"CLAUDE_CODE_OAUTH_TOKEN":…}}` message to the running CLI | **accepted**: `applied: ["CLAUDE_CODE_OAUTH_TOKEN"]`, so tokens can be rotated live |
| **E5** | E3o session; prompt sent from the claude.ai web app | the host stream got `system init` → assistant reply → `result success` |
| **E6** | E5 + `--permission-prompt-tool stdio --replay-user-messages`; web prompt needing `Write` | the host stream got the web **user prompt**. **`can_use_tool Write` came to the host**; the host allowed it and the file was written |
| **E7** | E6; prompt sent **from the host** | it appeared in the web app with its reply |

| **E8** | a ViberPit session launched on the slot recipe (E3o); a Bash tool runs `printenv \| grep -c '^CLAUDE_CODE_OAUTH_TOKEN='` (no scrub setting configured) | `0`: the CLI **scrubs `CLAUDE_CODE_OAUTH_TOKEN` from tool subprocess env by default**. `CLAUDE_CODE_ORGANIZATION_UUID` stays visible to tools, which is harmless (not a credential) |

E8 was verified live on 2026-11-24 against claude 2.1.331.

Conclusions:

- **Minimum recipe:** a full-scope claude.ai access token (needs `user:sessions:claude_code`)
  plus `CLAUDE_CODE_ORGANIZATION_UUID`.
- A setup-token can never work.
- The first-party-entrypoint host-refresh path (E2/E2b) is not needed. That's good, because it
  would mean claiming to be `local-agent`.
- Both sides can drive the session. Approvals arrive at the host as `can_use_tool`, which in
  ViberPit becomes the normal approval card.

## 3. Credential state today (metadata only)

`paths.slot_path(<account>)`:

| Account | Slot scopes | Access token | Refresh token |
|---------|-------------|--------------|---------------|
| ibaset-personal | full, incl. `sessions:claude_code` | expired | **expired 68 days ago** |
| tony_cairnea.com | full | expired | **expired 53 days ago** |
| tonystein85_gmail.com | full | expired | **expired 63 days ago** |

The slots lapsed after sessions moved to setup-tokens: nothing refreshes a slot whose account has
a setup-token. Each account needs **one fresh browser login** into its slot.

## 4. Design (Principle II compliant)

1. **Authority keeps slots fresh.** The dobby primary is the refresh authority
   (`accounts.is_refresh_authority`, `acquire_valid_token`). It must refresh the browser-login slot
   of every Remote-Control-enabled account even when a setup-token exists, so the refresh token
   never lapses. It is the only refresher, so there is no rotation race.
2. **Launch.** A session that has Remote Control effectively on, and whose account has a valid
   slot, launches with the slot's **access token** as `CLAUDE_CODE_OAUTH_TOKEN`, plus
   `CLAUDE_CODE_ORGANIZATION_UUID` from the account profile.
   - No refresh token reaches the CLI, so the CLI has nothing to rotate. This is the same shape as
     the #2629 `oauth_access` envelope.
   - Prefer `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`, which the CLI also supports, over the env
     var. Principle II asks that access material stay out of the environment.
   - **Decision (Task B): env delivery is accepted for now.** Two reasons:
     - The SDK has no hook to pass the CLI an inherited file descriptor; it would need a
       custom transport.
     - E8 shows the CLI scrubs `CLAUDE_CODE_OAUTH_TOKEN` from every tool subprocess by
       default, so the model's tools never see the token.

     FD delivery remains a follow-up.
   - Otherwise launch with the setup-token as today, and Remote Control shows as unavailable
     (PR #3462).
3. **Live rotation.** Before the access token expires (about 8 h), the authority pushes the
   refreshed token to every live session on that account with the top-level
   `update_environment_variables` message (E4). Runners get it through the existing envelope push.
   - **Built (runner follow-up).** The push carries a separate Remote Control envelope
     (`rc_envelopes`: access token + org UUID, own generations, never a refresh token). The
     inference envelope still prefers the setup-token. Each runner sync pushes a new token
     into that runner's live Remote Control CLIs.
   - **Principle II note.** Like the inference envelope (#2629), the runner caches it in a
     host-local 0600 file (`rc-envelopes.json`) so a daemon restart can relaunch before the
     next push. That departs from the constitution's "never written to … disk" for access
     material, and follows the existing envelope-cache precedent. It carries a short-lived
     access token only.
4. **Transcript parity.** Add `--replay-user-messages` so prompts typed in the Claude apps appear
   in ViberPit's transcript.
   - The driver must not double-render its own prompts, which are echoed back too: dedupe by
     message `uuid`, or skip echoes of prompts it sent.
5. **Approvals.** Nothing to build: `can_use_tool` already reaches ViberPit (E6). Open question:
   does the Claude app also show the request, and who wins when both answer? E6's host answered
   at once.

### What a `--replay-user-messages` echo carries (claude 2.1.331, read from the binary)

- The replay builders emit `{type:"user", uuid, isReplay:true, ...}`. They add
  `isSynthetic: true` for an entry the CLI marked `isMeta` (its own meta and synthetic
  prompts), `file_attachments`, and the entry's `origin` when it has one.
- The bridge origin (`bridgeOrigin` / `dispatchedOverBridge`) only feeds the CLI's
  `session.receive` hook classification (`{kind:"bridge"}`). No positive "typed in the
  Claude apps" marker was found on the stream echo. What an echo of a bridge prompt
  carries live is still unverified.
- `claude_agent_sdk`'s `parse_message` drops every one of these keys. The driver
  re-reads them from the raw stream (`claude_driver._with_cli_flags` → `cli_flags`).
- The translator renders a foreign echo as an app-typed prompt only when all of these
  hold:
  - the bridge is open (the CLI answered an enable);
  - the echo is not `isSynthetic`/`isMeta`;
  - it is not a slash command;
  - it is not a leading `<tag>` envelope.

### Still unverified

- A bridge across a **token expiry plus rotation**. E4 shows the CLI accepts the new token. That
  the bridge's worker JWT re-derives from it is inferred from the CLI code, not observed.
- What the Claude app shows while a ViberPit approval card is pending.
- A Copilot account stays ineligible: it has no claude.ai login.
- A bridge opened on a **runner** with the vended Remote Control envelope, and a live push
  across it, have not been observed. Runner launches use the same recipe (E3o), but rocky
  has not run it yet.

## 5. Operator action required

Log each account into its browser-OAuth slot once, through the account re-auth flow. Without a
live slot, the design falls back to the setup-token and Remote Control stays unavailable.
