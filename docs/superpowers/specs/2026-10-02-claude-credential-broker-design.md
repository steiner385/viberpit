# Claude credential broker — design

**Date:** 2026-10-02
**Status:** draft for review (step 3 of #2625); build tracked in #2629
**Origin:** #2625 — the cross-host token-refresh race was verified on the live
dobby/rocky mount, and the interim fix (a worker never calls the token endpoint)
landed with that issue. This spec is the permanent fix.

## Problem

Constitution Principle II allows at most one credential authority. Codex has one
(`server/codex/auth_authority.py`, #1178). Claude accounts do not: rocky reads and
writes every account's credentials through an sshfs mount of dobby's
`~/.claude-accounts` (`docs/runbooks/rocky-session-host.md` §1). Three processes
on two hosts rotate single-use Anthropic refresh tokens against one set of files:

| Refresher | Host | Serialised against the others? |
|---|---|---|
| primary daemon — idle-token sweep, usage poller, helper calls | dobby | `_refresh_guard` (`threading.Lock` + `flock`) — local to dobby |
| worker daemon — helper calls, recompose, (before #2625) idle-token sweep | rocky | same guard — local to rocky |
| `claude` CLI — rotates its own family in `cfg/<account>/.credentials.json` | the session's host | not at all |

### What #2625 step 1 established (live mount, 2026-10-02)

`flock` does **not** cross the sshfs mount in either direction. A lock held on
dobby's local filesystem is invisible to rocky through the mount and vice versa;
two rocky processes do exclude each other (the lock is enforced by the kernel
that opened the file). POSIX `lockf` is not honoured through the mount at all.
The mount runs `-o dir_cache=no,attr_timeout=0,entry_timeout=0`, so this is not
a caching artefact.

Both hosts were refreshing: dobby's daemon runs as primary beside the edge, and
rocky's as worker, and the idle-token sweep was started on both roles. The
interim fix moved the role half of the #852 poller gate into
`viberpit.accounts.is_refresh_authority()` so that **no** refresh path in a
worker process reaches the token endpoint (`ensure_valid_token` raises
`RefreshDeferred`; `usage_for` takes its read-only path; the sweep is skipped).

### What the interim fix does not cover

1. **The CLI's own rotation.** A `claude` process on rocky launched from a
   composed browser-OAuth blob refreshes that blob itself when it expires, in
   the cfg dir, with no lock at all. The slot then holds a consumed refresh
   token until `sync_back` copies the rotation over.
2. **The worker's slot read-modify-write.** `sync_back` runs on the worker after
   every turn and on every poll. It reads the slot, merges with the cfg dir's
   blob (`merge_keep_later`, which compares against the copy it *read*) and
   writes. A worker RMW that straddles a primary refresh write re-persists the
   superseded token over the fresh one. The primary's next refresh from that
   slot gets `invalid_grant`, which `oauth.refresh_access_token` treats as a dead
   grant, and the account flips to needs-reauth — the `kindash` shape from
   `2026-06-23-idle-account-token-durability-design.md`.
3. **The mount itself** — #1005, #825/#826, #2016, #2291, #779, #851 were each
   fixed at the mount level; the credential files are the last reason the mount
   must be read-write.

## Verified against the real binary

Claude Code 2.1.183 (the SDK-bundled binary rocky runs), 2026-10-02, scratch
`CLAUDE_CONFIG_DIR`, `-p` turn on haiku:

| Launch | Outcome | `.credentials.json` afterwards |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN=<setup token>`, empty cfg dir | success | **not written** |
| env token **and** a composed file whose `claudeAiOauth` blob is expired with a bogus refresh token | success | byte-for-byte untouched |
| only that file, no env token | `401 OAuth access token is invalid` | untouched (no rotation attempted) |

So a session launched with a setup token in its environment holds no refresh
token, performs no rotation, and ignores a composed blob entirely. That answers
the open question in #2625: runner sessions can be launched without any
rotatable credential, and the broker never has to re-inject anything mid-session.
Setup tokens are minted for one year (`setup_token_oauth._EXPIRES_IN`) and need
the `user:profile` scope for the usage endpoint — both already handled by the
paste-code reauth path.

## Goals

1. Exactly one process rotates a Claude account's token family: the authority
   on dobby. No other process holds a refresh token.
2. Rocky stops mounting `~/.claude-accounts` read-write. (Moving `~/.claude` —
   skills/hooks/plugins — off the live mount is a separate follow-up.)
3. A runner that loses the authority keeps running its live sessions and can
   still launch new ones for as long as the credentials it already holds are
   valid; it fails closed, never by inventing a credential.

Non-goals: changing how the primary itself refreshes (its `_refresh_guard` is
correct on one host); any change to Codex or Copilot auth; reviving dead tokens.

## Design

### 1. The authority (dobby)

Reuse the shape of `server/codex/auth_authority.py` rather than its code: the
Claude flow has no `codex app-server`, so the authority is the existing
`viberpit.accounts` refresh path running in **one** process. Concretely, the
primary daemon *is* the authority; no new systemd unit. What changes is the
contract:

- `ensure_valid_token` / `acquire_valid_token` / `usage_for`'s refresh branch
  are callable only where `is_refresh_authority()` is true (already enforced).
- The authority publishes, per account, a **credential envelope**:
  `{account, kind: "setup_token" | "oauth_access", token, expires_at, generation}`.
  `generation` is a persisted monotonic counter per account, exactly as the
  Codex authority uses it: a runner that is told to use generation *n* never
  accepts *n-1* again, and a refresh may only publish *n+1*.
- Envelope preference: a stored setup token (`crypto.read_oauth_token`) when
  the account has one; otherwise the current access token with its expiry.
  Today every Anthropic account in the fleet has a setup token, so in practice
  runners never see an `oauth_access` envelope; the second kind exists so an
  account that has only browser OAuth still works, at the cost of re-vending
  before expiry (see §3).

### 2. The transport (edge → runner, mTLS)

A worker has no client identity toward the primary daemon today
(`VIBERPIT_SESSIOND_CLIENT_ALLOWLIST=edge`); adding one would be a second mTLS
client surface. Instead the **edge** carries envelopes to runners over the RPC
channel it already has to every daemon (`FleetOrchestrator` → `DaemonClient`):

- `creds_sync(envelopes, lease_s)` — pushed on the fleet probe cadence (≤30 s)
  and immediately after any authority rotation or reauth. The runner stores the
  envelopes in memory and in a host-local, `0600`, authority-generation-stamped
  cache under `$XDG_STATE_HOME/viberpit/state/claude-creds/` (same locality
  rule as Codex homes, §4.1 of the rocky runbook), so a daemon restart does not
  need the edge to be up.
- The lease mirrors `AuthorizationSync`: envelopes expire `lease_s` after the
  last sync, so a silent edge fails closed — an account disabled while the
  edge was down cannot keep being used past the lease.
- The runner acknowledges with the highest generation it holds per account;
  the edge reports a runner that is behind as a fleet warning, not an error.

### 3. Launching sessions on a runner

- `configdir.compose` on a runner writes **no** `claudeAiOauth` blob; the
  env built by `server/sessions/account_auth.py` carries the envelope token as
  `CLAUDE_CODE_OAUTH_TOKEN`. For `setup_token` envelopes this is exactly the
  verified launch mode above.
- For an `oauth_access` envelope the runner refuses to *launch* when the token
  is within `expiry_skew_sec` of expiry (the edge will have re-vended by then)
  and lets a *running* session finish its turn; the orchestrator's existing
  in-session 401 path then relaunches with the newest envelope. This is the
  only place the design is weaker than today, and only for accounts with no
  setup token — the account page should say so.
- `sync_back` is a no-op on a runner: there is nothing in the cfg dir to copy
  back, and the slot is not reachable. The usage poller on a runner reads
  ledger state the primary wrote (as the #852 read-only poll already does).

### 4. Decommissioning the mount

Once every runner launches from envelopes, `~/.claude-accounts` on rocky is
unmounted. The runner still needs: the account manifest (`accounts.json`, read
only — ship it in `creds_sync`, it is already non-secret and versioned), the
ledger (read for rotation decisions; the runner's writes move to the existing
daemon RPC that reports usage to the edge), and attachments/archives (out of
scope; they stay on the hdd-storage mount, which is not credential-bearing).

## Components

- `viberpit/credential_envelope.py` — dataclass + generation rules (pure).
- `server/sessions/claude_authority.py` — envelope minting on the primary from
  the existing slot/setup-token readers; generation persistence beside the slot.
- `server/ipc/creds_rpc.py` — `creds_sync` / `creds_ack` on the daemon; runner
  cache under the state dir.
- `server/sessions/fleet_orchestrator.py` — push on probe + on rotation.
- `server/sessions/account_auth.py` / `viberpit/configdir.py` — runner launch
  from an envelope; no composed blob on a runner.
- `docs/runbooks/rocky-session-host.md` — §1 and §4 rewritten for the
  no-mount runner.

## Testing

- Envelope generation monotonicity and rollback refusal (pure).
- Runner fails closed when the lease lapses; keeps serving live sessions.
- `compose` on a runner never writes `claudeAiOauth`; the launch env carries the
  envelope token; `sync_back` is a no-op.
- The existing `is_refresh_authority` suite stays: a runner process must never
  reach the token endpoint, with or without the mount.
- Live verification per Principle III before cut-over: one session on rocky
  from a setup-token envelope with the mount **absent**, through a full turn,
  a helper call (title generation) and an archive.

## Acceptance (#2629)

- [ ] No process other than the primary daemon on dobby holds or rotates an
      Anthropic refresh token; `grep` confirms no runner code path reads a
      `*.cred.json` slot.
- [ ] Rocky runs sessions with `~/.claude-accounts` unmounted.
- [ ] A token rotated on dobby is usable on rocky within one probe interval.
- [ ] A silent edge stops a runner from launching new sessions after the lease
      lapses, and does not kill running ones.
- [ ] Runbook §1/§4 describe the no-mount runner; the remount recipe is gone.

## Risks

- Accounts without a setup token depend on timely re-vending; mitigated by
  preferring setup tokens and by the fleet warning for a behind runner.
- `CLAUDE_CODE_OAUTH_TOKEN` precedence over a composed file is observed
  behaviour of 2.1.183, not a documented contract. Re-verify on every bundled
  CLI bump (the eval that pins `claude --version` is the place).
- The ledger split (primary writes, runner reads via RPC) touches the rotation
  monitor's inputs; keep it behind the existing `#852` read-only semantics and
  land it as its own PR.
