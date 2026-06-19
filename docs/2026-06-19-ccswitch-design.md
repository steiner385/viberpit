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
- **Detection surface.** Usage limits arrive as HTTP **429** with a `retry-after` header and a
  **`resetsAt`** epoch (ms) in the body. `resetsAt` is the *authoritative* signal for when an
  account becomes usable again. (The CLI internally models account switching on rate-limit via
  `tengu_rate_limit_options_menu_select_switch`.)
- **Token sync-back hazard.** Claude auto-refreshes the access token and **writes it back** to
  the active config dir's `.credentials.json`. Any tool that swaps credentials MUST sync the
  refreshed token back into that account's stored slot afterward, or a later swap will clobber a
  good token with a stale one.

## Non-goals (YAGNI)

- No long-running coordinator daemon in v1. Per-process `CLAUDE_CONFIG_DIR` selection plus a
  flock-guarded ledger gives most of the cross-job coordination benefit. A daemon can be added
  later **only if** observed contention proves it necessary.
- No attempt to read an exact "remaining quota" from Anthropic — no such API exists for Max.
  Headroom is *estimated* from measured token usage and *calibrated* from where real 429s land.
- No mid-process hot-swap of a live session's in-memory token (fragile and unnecessary).

## Architecture overview

A small Python 3 (stdlib-only) package with one core library and two thin client commands:

| Component        | Role                                                                                  |
|------------------|---------------------------------------------------------------------------------------|
| `ccswitch` (lib) | Account registry, credential split/merge + sync-back, flock-guarded usage ledger, `select_best()`, config-dir composition/reconciliation. |
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

### 2. Usage ledger & estimation

`ledger.json` (flock-guarded; contains no secrets), per account:

```jsonc
{
  "accounts": {
    "alpha": {
      "status": "ok | cooling | needs-reauth",
      "session": { "resetsAt": 0, "estTokens": 0 },   // 5-hour rolling window
      "weekly":  { "resetsAt": 0, "estTokens": 0 },
      "lastObserved429": { "scope": "session|weekly", "resetsAt": 0 },
      "subscriptionType": "max",
      "reservationUntil": 0
    }
  },
  "learnedBudgets": { "session": null, "weekly": null }   // calibrated tokens-per-window
}
```

- **Authoritative cooldown.** On an observed 429, record `scope` + `resetsAt` and mark the account
  `cooling` until `resetsAt`. Scope (5-hour session vs weekly) is taken from the response when
  present, otherwise inferred from reset distance (short → session, long → weekly).
- **Measured usage between resets.** Token counts come from the real `usage` field of each
  invocation — present in both the headless result JSON and the interactive session JSONL — summed
  into the current window. This is *measured*, not guessed.
- **Self-calibration.** Each real 429 records the `estTokens` accumulated when the wall was hit,
  refining `learnedBudgets[scope]`. Over time the proactive threshold sharpens toward the true
  (unpublished) per-tier limit.
- **`select_best()`** drops `cooling` accounts whose `resetsAt` is in the future and whose
  `reservationUntil` has not lapsed, ranks survivors by estimated headroom
  `min(sessionBudget − sessionEst, weeklyBudget − weeklyEst)`, and writes a short-TTL
  `reservationUntil` (a soft reservation) on the winner so concurrent selectors don't stampede the
  same account. Tie-break: round-robin by least-recently-used.
  **Cold start:** until `learnedBudgets[scope]` is calibrated (still `null`), headroom is
  incomputable, so selection falls back to round-robin among non-cooling accounts and relies purely
  on the reactive 429 path; proactive switching activates only once a budget has been learned.
- **Proactive (Hybrid) switch.** A client may pre-emptively avoid an account whose estimated
  window usage exceeds a configurable fraction (default 0.9) of `learnedBudgets[scope]`, treating
  the estimate as a hint. The authoritative 429 path is always the safety net.

### 3. Interactive flow — `ccmax [claude args…]`

1. `ccswitch link` reconcile the candidate config dir.
2. `account = select_best()`; compose its `.credentials.json`.
3. Spawn `claude` with `CLAUDE_CONFIG_DIR=cfg/<account>` in the foreground, inheriting the TTY.
4. A sidecar tails the newest session JSONL under shared `projects/` for the cwd, detects a
   rate-limit event (carrying `resetsAt`), and captures the session id.
5. **On limit (auto-relaunch, the default):** record the 429 in the ledger, pick the next-best
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

1. `select_best()` → compose config dir.
2. Run `claude -p` with structured output (`--output-format json`, or `stream-json`) so `usage` and
   errors parse cleanly. If the caller specifies their own output format, respect it but still parse
   exit status / stderr for the limit signal.
3. **Success:** record measured tokens into the ledger; sync-back creds.
4. **429 / limit:** record the authoritative reset, exclude that account, and **retry the same
   prompt** on the next-best account, up to `#accounts` attempts.
5. **All cooling:** exit non-zero reporting the earliest `resetsAt` (rendered in America/New_York).
   `--wait` optionally sleeps until that instant and then proceeds.

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

- **Unit:** `select_best` (cooling filter, headroom ranking, reservation TTL, round-robin
  tie-break); credential split/merge and sync-back (keep-later-`expiresAt`); calibration update.
- **Fake-`claude` stub:** a stub executable emitting scripted outputs — success with a `usage`
  block, then a 429 with `resetsAt` — so `ccrun` rotation/retry and `ccmax` kill+`--resume` are
  tested **without burning real quota**. Selected via `CCSWITCH_CLAUDE_BIN` override.
- **Concurrency:** spawn N parallel `ccrun` against the stub; assert no two select the same
  near-exhausted account and the ledger stays consistent under flock.
- **Manual smoke:** real `ccswitch add` for one account → `ccrun -- claude -p "hi"` → confirm
  sync-back bumped `expiresAt` in that account's slot.

## Stack & packaging

- Python 3, **stdlib only** (`json`, `fcntl` for flock, `subprocess`, `pathlib`, `argparse`,
  `datetime`, `zoneinfo`). Zero third-party dependencies.
- Single package `ccswitch/`; `ccswitch`, `ccmax`, `ccrun` exposed as console scripts installed to
  `~/.local/bin`.
- Config knobs (proactive threshold, reservation TTL, default timezone, `claude` binary path) via a
  small `~/.claude-accounts/config.json` with sane defaults.

## Open questions resolved

- **3a — interactive limit behavior:** auto-relaunch with `--resume` by default; `--confirm-switch`
  to prompt instead. *(Approved.)*
- **Repo location:** standalone repo at `~/GitHub/cc-account-switcher`, spec under `docs/`.
  *(Approved.)*
