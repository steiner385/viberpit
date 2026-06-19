# ccswitch — Automatic Claude Max Account Rotation — Design

**Date:** 2026-06-19
**Status:** Approved design, pre-implementation
**Author:** Tony Stein (with Claude Code)

## Problem

I hold three Claude Max accounts and manually shuffle between them to stay within
per-account session (5-hour rolling) and weekly usage limits. The goal is a tool that
rotates between these accounts **automatically, without my intervention**, so I never hit
a hard service interruption: when one account approaches or hits its limit, work continues
on another account that still has headroom.

Constraints discovered during design (verified against Claude Code CLI v2.1.183 on Linux):

- **Credential location.** On Linux, Claude Code stores the Max subscription token in
  `~/.claude/.credentials.json` under the `claudeAiOauth` key
  (`accessToken`, `refreshToken`, `expiresAt`, `scopes`, `subscriptionType`, `rateLimitTier`).
  The same file also holds an `mcpOAuth` map (per-MCP-server logins: Linear, Notion, Sentry,
  Stripe), which is **account-agnostic**.
- **`CLAUDE_CONFIG_DIR` is supported and load-bearing.** The CLI resolves its config dir as
  `process.env.CLAUDE_CONFIG_DIR ?? ~/.claude`, and `.credentials.json` resolves *inside* that
  dir. Therefore an account can be selected **per process** by setting one env var — no need to
  mutate a shared global file, and no race between concurrently-running `claude` processes.
- **Exact usage is available without launching `claude`.** `GET https://api.anthropic.com/api/oauth/usage`
  (headers `Authorization: Bearer <accessToken>` and `anthropic-beta: oauth-2025-04-20`) returns the
  account's precise usage. Verified live response shape:
  ```jsonc
  {
    "five_hour": { "utilization": 11.0, "resets_at": "2026-06-19T18:00:00Z", ... },   // session (5h)
    "seven_day": { "utilization": 69.0, "resets_at": "2026-06-22T20:00:00Z", ... },   // weekly (all)
    "seven_day_opus":   null,
    "seven_day_sonnet": { "utilization": 9.0, "resets_at": "..." },
    "extra_usage":      { "is_enabled": false, "disabled_reason": "out_of_credits", ... },
    "limits": [   // normalized, forward-compatible form — prefer this
      { "kind": "session",      "group": "session", "percent": 11, "severity": "normal", "resets_at": "...", "is_active": false },
      { "kind": "weekly_all",   "group": "weekly",  "percent": 69, "severity": "normal", "resets_at": "...", "is_active": true  },
      { "kind": "weekly_scoped","group": "weekly",  "percent": ...,                       "resets_at": "..." }
    ]
  }
  ```
  `utilization` is a percentage (0–100). This is the **authoritative** headroom signal and removes
  any need to estimate usage.
- **Independent token refresh.** Idle accounts' access tokens expire, and the CLI only auto-refreshes
  the *active* account. The manager refreshes a stored token itself:
  `POST https://platform.claude.com/v1/oauth/token` with
  `{ grant_type: "refresh_token", refresh_token: <stored>, client_id: "9d1c250a-e61b-44d9-88ed-5944d1962f5e" }`
  (the public Claude Code OAuth client; scopes `user:inference user:profile org:create_api_key`),
  persisting the new token set back to the account slot.
- **Real-time detection (safety net).** Live limits still arrive as HTTP **429** with a `retry-after`
  header and a `resets_at` value. This remains a secondary signal for the case where usage crosses
  100% mid-request between polls. (The CLI internally models account switching on rate-limit via
  `tengu_rate_limit_options_menu_select_switch`.)
- **Token sync-back hazard.** Claude auto-refreshes the access token and **writes it back** to
  the active config dir's `.credentials.json`. Any tool that swaps credentials MUST sync the
  refreshed token back into that account's stored slot afterward, or a later swap will clobber a
  good token with a stale one.

## Non-goals (YAGNI)

- No long-running coordinator daemon in v1. Per-process `CLAUDE_CONFIG_DIR` selection plus a
  flock-guarded ledger gives most of the cross-job coordination benefit. A daemon can be added
  later **only if** observed contention proves it necessary.
- No usage *estimation* or self-calibration: the `/api/oauth/usage` endpoint provides exact
  utilization, so the earlier token-counting/learned-budget machinery is dropped entirely.
- No mid-process hot-swap of a live session's in-memory token (fragile and unnecessary).

## Architecture overview

A small Python 3 (stdlib-only) package with one core library and two thin client commands:

| Component        | Role                                                                                  |
|------------------|---------------------------------------------------------------------------------------|
| `ccswitch` (lib) | Account registry, credential split/merge + sync-back, independent OAuth token refresh, exact-usage polling (`/api/oauth/usage`) with a short-TTL cache, flock-guarded state, `select_best()`, config-dir composition/reconciliation. |
| `ccswitch` (CLI) | `add` / `remove` / `list` / `link` — capture-on-login and store management.           |
| `ccmax`          | Interactive launcher: pick best account, launch `claude` on the TTY, auto-relaunch with `--resume` on a different account when a 429 is detected. |
| `ccrun`          | Headless wrapper: pick best account, run `claude -p …`, retry the same prompt on the next-best account on a 429. Built for unattended background jobs. |

Selection is **per process** via `CLAUDE_CONFIG_DIR`; shared state is **flock-guarded**.

### 1. Account store layout

```
~/.claude-accounts/              # mode 0700, gitignored
  store/<name>.cred.json         # mode 0600: ONLY that account's claudeAiOauth blob
  store/mcp.shared.json          # mode 0600: shared mcpOAuth (account-agnostic MCP logins)
  cfg/<name>/                    # a real CLAUDE_CONFIG_DIR per account
      .credentials.json          #   composed = shared mcpOAuth ⊕ <name>'s claudeAiOauth
      projects -> ~/.claude/projects     # symlink (shared) — required for --resume across a switch
      plugins  -> ~/.claude/plugins      # symlink (shared)
      settings.json      -> ~/.claude/settings.json       # symlink (shared)
      settings.local.json -> ~/.claude/settings.local.json
      <every other ~/.claude entry except .credentials.json> -> symlink (shared)
  ledger.json                    # usage/cooldown state (NO secrets), flock-guarded
```

**Credential split.** `.credentials.json` mixes account-specific `claudeAiOauth` with
account-agnostic `mcpOAuth`. The manager stores them separately:
- `store/<name>.cred.json` holds only that account's `claudeAiOauth`.
- `store/mcp.shared.json` holds the shared `mcpOAuth`.
- A config dir's `.credentials.json` is **composed** by merging the two
  (`{ "mcpOAuth": <shared>, "claudeAiOauth": <account> }`).

This avoids re-authenticating four MCP servers per account and lets an MCP-token refresh in any
account propagate to all.

**Reconciler.** `ccswitch link [--all]` rebuilds the symlink mirror for a config dir idempotently
(symlinks every top-level `~/.claude` entry except `.credentials.json`). It runs on `add` and
cheaply before every launch, absorbing any new state files Anthropic introduces. Files Claude
writes through a shared symlink (e.g. `projects/<session>.jsonl`) land in shared storage, so
`--resume` works after an account switch.

### 2. Usage source & selection (exact, via `/api/oauth/usage`)

Headroom is read directly from Anthropic — no estimation. The manager keeps a small flock-guarded
state file `ledger.json` (no secrets) that is purely a **cache + concurrency** aid:

```jsonc
{
  "accounts": {
    "alpha": {
      "status": "ok | cooling | needs-reauth",
      "usage": {                              // last fetched from /api/oauth/usage
        "fivehourPct": 11.0,  "fivehourResetsAt": "2026-06-19T18:00:00Z",
        "weeklyPct":   69.0,  "weeklyResetsAt":   "2026-06-22T20:00:00Z",
        "extraUsageEnabled": false,
        "fetchedAt": "2026-06-19T15:42:00Z"
      },
      "subscriptionType": "max",
      "reservationUntil": 0
    }
  }
}
```

- **Polling.** `get_usage(account)` GETs `/api/oauth/usage` with that account's token and parses the
  `limits[]` array (preferred) — `session`/`weekly_all` entries give `percent`, `resets_at`,
  `is_active` — falling back to the `five_hour`/`seven_day` objects. Results are cached for a
  configurable TTL (default 60s) to avoid hammering the endpoint when many jobs select at once.
- **Token validity.** Before polling, if an account's `accessToken` is expired or within a skew
  window of `expiresAt`, the manager refreshes it first (independent OAuth refresh, see constraints)
  and persists the new token to the account slot. A 401 from the usage GET triggers one refresh +
  retry; a second failure marks the account `needs-reauth`.
- **`select_best()`** polls each account (respecting cache), then:
  1. Excludes accounts that are `needs-reauth`, or whose session **or** weekly utilization is at/above
     the hard ceiling (default 100% — i.e. effectively cooling until the relevant `resets_at`), or
     whose `reservationUntil` has not lapsed.
  2. Among survivors, ranks by **most headroom** = lowest session utilization, with weekly
     utilization as a secondary sort (an account near its weekly cap is deprioritized even if its
     5-hour window is fresh).
  3. Writes a short-TTL `reservationUntil` (soft reservation) on the winner so concurrent selectors
     don't stampede the same account.
- **Proactive (Hybrid) switch.** Clients avoid an account whose session utilization is ≥ a
  configurable soft threshold (default **90%**) when a less-loaded account exists — switching
  *before* the wall. The hard ceiling (100%) and the live 429 path (§3/§4) are the safety nets for
  usage that crosses mid-request between polls.
- **All accounts at/above ceiling.** Report the earliest relevant `resets_at` (America/New_York);
  `ccrun --wait` may sleep until then.

### 3. Interactive flow — `ccmax [claude args…]`

1. `ccswitch link` reconcile the candidate config dir.
2. `account = select_best()`; compose its `.credentials.json`.
3. Spawn `claude` with `CLAUDE_CONFIG_DIR=cfg/<account>` in the foreground, inheriting the TTY.
4. **Mid-session monitoring (the core of unattended operation).** The interactive `claude` process
   does **not** exit when it hits a limit mid-turn — it shows a rate-limit menu and waits — so
   detection must come from *outside* the process. A sidecar runs two detectors concurrently:
   - **Proactive poll:** every `pollInterval` (default 60s) it calls `/api/oauth/usage` for the
     active account; when session utilization crosses the soft threshold (default 90%) **and** a
     less-loaded account exists, it pre-empts at the next safe boundary.
   - **Hard safety net:** it tails the newest session JSONL under shared `projects/` for the cwd and
     catches the actual rate-limit event (carrying `resets_at`) the instant a 429 lands, for the
     case where a single turn blows past the threshold between polls.
   Either trigger captures the active session id and fires the switch.
5. **On limit (auto-relaunch, the default):** record the reset in the ledger, pick the next-best
   account, **SIGTERM the current `claude`**, and relaunch `claude --resume <session-id>` under the
   new config dir, printing a banner: *"Account alpha hit its limit (resets 3:20 PM ET). Switching
   to beta and resuming…"*. `--confirm-switch` makes this prompt first instead of switching
   automatically.
6. **On normal exit:** sync-back creds (split live `.credentials.json` → account slot + shared
   mcpOAuth), update the ledger.

**Accepted tradeoff:** kill + `--resume` reconstructs from the transcript and drops any unsubmitted
input or in-flight tool turn. This is acceptable for unattended operation; `--confirm-switch` is the
opt-out.

### 4. Headless flow — `ccrun -- claude -p "…"`

The natural fit for many parallel background jobs; fully unattended.

1. `select_best()` (exact usage, excludes accounts at/above ceiling) → compose config dir.
2. Run `claude -p` with structured output (`--output-format json`, or `stream-json`) so errors parse
   cleanly. If the caller specifies their own output format, respect it but still parse exit status /
   stderr for the limit signal.
3. **Success:** sync-back creds (the just-used account's usage is refreshed on the next poll).
4. **429 / limit:** record the authoritative `resets_at`, exclude that account, and **retry the same
   prompt** on the next-best account, up to `#accounts` attempts.
5. **All at/above ceiling:** exit non-zero reporting the earliest `resets_at` (America/New_York).
   `--wait` optionally sleeps until that instant and then proceeds.

Because a single headless invocation is one request/response, mid-run exhaustion simply surfaces as
the 429 in step 4 and is retried on another account — there is no long-lived session to interrupt.

### 5. Capture-on-login & store management — `ccswitch …`

- **`add <name>`** — create the account dir, run `claude /login` with `CLAUDE_CONFIG_DIR` pointed at
  a temporary capture dir (so live sessions are undisturbed); after browser login completes, read
  the resulting `.credentials.json`, split `claudeAiOauth` → `store/<name>.cred.json` and merge
  `mcpOAuth` → `store/mcp.shared.json`; record `subscriptionType`/`rateLimitTier` and **warn if not
  Max**; verify with a cheap `claude -p "ok"` ping.
- **`list`** — accounts with status, estimated headroom, and reset clocks (America/New_York).
- **`remove <name>`** — delete the account's store slot and config dir.
- **`link [--all]`** — rebuild config-dir symlink mirrors idempotently.

### 6. Error & edge handling

- **All accounts cooling** → report earliest reset (ET); `ccrun --wait` may sleep until then;
  `ccmax` prints the wall-clock and exits.
- **Refresh fails (401 after refresh)** → mark account `needs-reauth`, skip in selection, instruct
  the user to `ccswitch add <name>` again.
- **Concurrent swaps** → structurally impossible: account selection is per-process via
  `CLAUDE_CONFIG_DIR`; ledger writes are flock-guarded; soft reservations prevent thundering-herd
  onto one account.
- **Same-account sync-back race** (two processes on one account) → keep the credential with the
  later `expiresAt` (refresh is monotonic).
- **Tier mismatch** (a Pro/Team account logged in) → warn at `add` and in `list`.
- **Security** → never log token values; store dir `0700`, credential files `0600`; the ledger
  holds no secrets. Reset times always rendered in America/New_York.

### 7. Testing strategy

- **Unit:** `select_best` (ceiling exclusion, headroom ranking by session-then-weekly utilization,
  reservation TTL); usage parsing from `limits[]` with `five_hour`/`seven_day` fallback; credential
  split/merge and sync-back (keep-later-`expiresAt`); token-refresh persistence.
- **Mock usage + token endpoints:** a local stub HTTP server (or injected fetch) returning scripted
  `/api/oauth/usage` payloads (low util → high util → at ceiling) and `/v1/oauth/token` refresh
  responses, so selection, proactive switching, the 401→refresh→retry path, and `needs-reauth` are
  tested deterministically.
- **Fake-`claude` stub:** a stub executable emitting scripted outputs — success, then a 429 with
  `resets_at` — so `ccrun` rotation/retry and `ccmax` kill+`--resume` are tested **without burning
  real quota**. Selected via `CCSWITCH_CLAUDE_BIN` override.
- **Concurrency:** spawn N parallel `ccrun` against the stub; assert no two select the same
  near-exhausted account and the ledger stays consistent under flock.
- **Manual smoke:** real `ccswitch add` for one account → `ccrun -- claude -p "hi"` → confirm
  sync-back bumped `expiresAt` in that account's slot.

## Stack & packaging

- Python 3, **stdlib only** (`json`, `fcntl` for flock, `subprocess`, `pathlib`, `argparse`,
  `datetime`, `zoneinfo`). Zero third-party dependencies.
- Single package `ccswitch/`; `ccswitch`, `ccmax`, `ccrun` exposed as console scripts installed to
  `~/.local/bin`.
- Config knobs via a small `~/.claude-accounts/config.json` with sane defaults: `softThreshold`
  (0.90), `hardCeiling` (1.00), `usageCacheTtlSec` (60), `pollIntervalSec` (60), `reservationTtlSec`,
  `expiryskewSec`, `timezone` (America/New_York), `claudeBin`.

## Open questions resolved

- **3a — interactive limit behavior:** auto-relaunch with `--resume` by default; `--confirm-switch`
  to prompt instead. *(Approved.)*
- **Repo location:** standalone repo at `~/GitHub/cc-account-switcher`, spec under `docs/`.
  *(Approved.)*
- **Exact usage source:** use `GET /api/oauth/usage` (verified live) for precise per-account
  utilization instead of estimating from token counts; the estimation/calibration subsystem is
  removed. Mid-session exhaustion is handled by a sidecar that polls usage on a timer *and* tails
  the session JSONL for the live 429. *(Approved.)*
