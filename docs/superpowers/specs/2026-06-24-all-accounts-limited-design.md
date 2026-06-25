# Design: Disambiguate "All accounts at limit"

**Date:** 2026-06-24
**Status:** Approved (brainstorming) — ready for implementation plan

## Problem

The dashboard shows an amber banner — **"All accounts at limit · No reset time
available"** — when no account is actually exhausted. Sessions launch and run
fine in the same view.

### Root cause

Session creation calls `select_account` → `pick_account` → `select_best` →
`rank_accounts`. `rank_accounts` (`src/ccswitch/selector.py:65`) builds a
candidate list, skipping any account that is `in exclude`, `in reserved`,
`u is None`, or `at_ceiling`. When the list is empty it raises
`AllAccountsBusy(earliest_reset)`. The API (`server/api/sessions.py:315`) turns
that into a `409 { code: "all_accounts_limited", earliestReset }`, and the
frontend (`Dashboard.tsx:1318`, `DesktopShell.tsx:296`) pins
`AllLimitedBanner`.

`earliest_reset` is **only** populated from the `at_ceiling` branch
(`selector.py:84-90`). So an empty candidate list with `earliest=None` means the
accounts were dropped for some reason *other than* being exhausted — most often
because `usage_for()` returned `None`.

`usage_for()` (`src/ccswitch/accounts.py:97`) returns `None` for several
**non-exhaustion** conditions: a transient token-refresh throttle/5xx
(`TransientError`), a usage-fetch failure, or a live-session token temporarily
owned by the SDK. A known trigger is Cloudflare 1010 (403) blocking the
header-less token refresh, which makes *all* accounts return `None` at once —
looking like "everyone is busy" when nobody is exhausted.

Two defects compound:

1. **Conflated conditions.** `AllAccountsBusy` collapses "genuinely at ceiling",
   "usage temporarily unreadable", and "credentials dead (needs-reauth)" into one
   misleading message. Worse, `usage_for()`'s bare `Usage | None` return loses the
   distinction between "transient blip, account is fine" and "refresh token is
   dead".
2. **Sticky banner.** Once shown, `AllLimitedBanner` persists until the user
   clicks ✕; it never re-validates against account recovery, so it goes stale.

## Goals

- Stop showing "all at limit" when accounts are merely unreadable.
- Let session creation succeed when usage can't be verified but accounts appear
  healthy.
- Keep an accurate, self-clearing banner for genuine exhaustion.
- Surface a distinct, actionable message when accounts genuinely need re-auth.

## Non-goals (YAGNI)

- No new SSE/event stream — reuse the existing 30s `useAccounts()` poll.
- No changes to in-session rotation/pause recovery (`_rotate_away` /
  `_pause_session` already handle real 429s).
- No retry/backoff added to the usage fetch itself.

## Design

### Three terminal states

The fix splits the single `AllAccountsBusy` into three outcomes:

| State | Detection | Behavior |
|---|---|---|
| **A. Genuinely at ceiling** | ≥1 account `at_ceiling` with a known `*_resets_at`; none eligible | Block `409 all_accounts_limited` + `earliestReset` → real banner "Capacity returns at X" |
| **B. Usage unreadable** | ≥1 healthy account (not needs-reauth/quarantined) whose `usage_for()` returned `None` | **Optimistically create** on it (last-resort priority); response flags `usageUnverified: true` → subtle toast |
| **C. No usable accounts** | every account is needs-reauth/quarantined; none at-ceiling, none unreadable-healthy | Block `409 accounts_need_reauth` → toast pointing at the Accounts tab |

### Backend

**`pick_account` (`src/ccswitch/accounts.py:193`)** already reads the ledger for
reservations. It will additionally collect each account's ledger `status` and
pass an `unhealthy: set[str]` (needs-reauth / quarantined) into selection,
alongside the existing `usages` and `reserved`.

**`rank_accounts` (`src/ccswitch/selector.py:65`)** gains a last-resort tier:

- Readable, below-ceiling accounts keep their existing composite ordering
  (perishability ÷ load).
- An account with `u is None and name not in unhealthy and not in exclude/reserved`
  becomes an **optimistic candidate**, ordered strictly *after* every readable
  candidate, and among optimistic candidates by least live `load` (the `load`
  dict the orchestrator already passes). A known-good account therefore always
  wins over a guess.
- The returned best carries an `optimistic` flag (usage unknown).
- When no candidate exists at all, raise `AllAccountsBusy(kind, earliest_reset)`
  where `kind` is `"at_ceiling"` (earliest set) or `"no_usable_accounts"`.

The `at_ceiling` test (`session_pct >= ceiling or weekly_pct >= ceiling`) remains
the single source of truth. The "unhealthy" set is derived from existing ledger
status — no new status vocabulary.

### API (`server/api/sessions.py`)

- **State B (optimistic success):** create normally → `201`, response body gains
  additive `usageUnverified: true`. Backwards compatible.
- **State A:** `409 { error.code: "all_accounts_limited", earliestReset }` —
  unchanged wire shape.
- **State C:** `409 { error.code: "accounts_need_reauth", message }` (no
  `earliestReset`).

### Frontend (`web/src/components/Dashboard.tsx` + `DesktopShell.tsx`)

Both shells carry their own `allLimitedReset` state and 409 handler; both get the
same treatment. Reuse `web/src/lib/formatReset.ts` — no duplication.

1. **`usageUnverified` toast (B):** in `createSession`'s `onSuccess`, if
   `session.usageUnverified`, fire a subtle non-blocking toast — *"Couldn't
   verify account usage — started anyway."* — then open the session as normal.
2. **`accounts_need_reauth` branch (C):** add a third `code` case to the existing
   409 handler (which already special-cases `branch_switch_failed`) → toast
   directing to the Accounts tab. Not the amber banner.
3. **`all_accounts_limited` (A):** unchanged → set `allLimitedReset`, show
   `AllLimitedBanner`.
4. **Auto-clear on recovery:** an effect subscribed to `useAccounts()` (already
   polling every 30s, `web/src/api/hooks.ts:34`) clears `allLimitedReset` to
   `undefined` as soon as any account reports `rank !== null` (selectable again)
   or its at-ceiling reset time has passed. The manual ✕ stays as a fallback.

`Account.rank` (`web/src/api/types.ts:28`) is non-null exactly when the account
is a selectable candidate, making it the recovery signal — no new endpoint.

## Testing (TDD: red → green per task)

**`src/ccswitch/selector.py` unit tests** — the matrix:
- all readable + below ceiling → normal pick, not optimistic.
- all at-ceiling → `AllAccountsBusy(kind="at_ceiling")`, `earliest_reset` set.
- all unreadable-healthy → optimistic pick = least-load, `optimistic=True`.
- all unhealthy (needs-reauth) → `AllAccountsBusy(kind="no_usable_accounts")`.
- mixed (some at-ceiling + some unreadable-healthy) → optimistic pick wins, no
  raise.
- readable + unreadable mix → readable wins over optimistic.

**`server/tests/test_api.py`** — extend existing 409 cases:
- optimistic create → `201` + `usageUnverified: true`.
- state C → `409 accounts_need_reauth`.
- state A → still `409 all_accounts_limited` + `earliestReset` (existing tests
  stay green).

**Web tests (`Dashboard` and `DesktopShell`):**
- `AllLimitedBanner` auto-clears when `useAccounts` yields a ranked account.
- `usageUnverified` toast fires on optimistic success.
- `accounts_need_reauth` routes to a toast, not the banner.

## Affected files

- `src/ccswitch/selector.py` — three-state ranking + `AllAccountsBusy.kind`.
- `src/ccswitch/accounts.py` — thread ledger status / `unhealthy` set, propagate
  `optimistic`.
- `server/sessions/selection.py` — pass through `optimistic` / `kind`.
- `server/sessions/orchestrator.py` — propagate optimistic flag from create path
  (rotation path `_rotate_away` unchanged).
- `server/api/sessions.py` — `usageUnverified`, `accounts_need_reauth`.
- `web/src/api/types.ts` — `usageUnverified` on the session create response.
- `web/src/components/Dashboard.tsx`, `web/src/components/DesktopShell.tsx` —
  toast branches + auto-clear effect.
- Tests as above.
