# Accounts screen: live session counts + next-pick priority rank

**Date:** 2026-06-23
**Status:** Approved (design)
**Feature area:** 001-ccswitch-web — Accounts screen

## Problem

The Accounts screen lists each account with its status and SESSION / WEEKLY usage
bars, but gives no visibility into two things the user cares about:

1. **How many sessions are currently using each account** — there is no per-account
   concurrency signal, so it's impossible to see at a glance which account is carrying
   load right now.
2. **The rotation algorithm's next-pick order** — `ccswitch` auto-selects accounts by a
   usage/perishability/load formula, but that ordering is invisible. The user wants to
   see which account would be chosen next, and the 2nd/3rd-place picture behind it.

## Goals

- Show a per-account live session count, split into **active** and **idle**.
- Show each eligible account's **priority rank** (`#1 next`, `#2`, …) as produced by the
  real selection algorithm, with at-ceiling/ineligible accounts marked unavailable.
- Surface a short **reason** for each rank via a hover tooltip.
- Single source of truth: the rank must come from the same code that actually selects
  accounts (constitution Principle VI) — no re-implementing the formula in TypeScript.

## Non-goals

- No change to the selection *algorithm* itself — only exposing its ordering.
- No new tooltip UI dependency (use the native `title` attribute).
- No reordering of the Accounts list (rank is shown as a badge; list order unchanged).
- No historical/aggregate session stats — counts are the current live snapshot only.

## Decisions (from brainstorming)

| Question | Decision |
| --- | --- |
| Session count basis | **Active + idle, shown split** (e.g. `2 active · 1 idle`) |
| Priority UI | **Rank badge per account** (`#1 next`, `#2`, …); list order unchanged |
| Reasoning | **Tooltip with reason** on the rank badge |

## Architecture

Data originates in the Python core/server and is exposed through the existing
`GET /api/accounts` response; the frontend renders two new presentational pieces. The
ranking logic is extracted (not duplicated) from the existing selector.

### 1. Selector — expose the full ordering

**File:** `src/ccswitch/selector.py`

Today `select_best()` builds a `candidates` list, sorts it, and returns the top name.
Refactor so the ordering is reusable:

- Add `rank_accounts(usages, reserved, cfg, now, exclude=frozenset(), load=None)
  -> RankResult` where `RankResult` carries:
  - `ranked`: list of eligible accounts in priority order, each a small record with
    `name`, `rank` (1-based), the sort-key components (`composite`, `load`,
    `session_pct`, `weekly_pct`), and a short `reason` string.
  - `unavailable`: list of at-ceiling/excluded account names (rank `None`), each with
    its binding reset time (for parity with the existing `AllAccountsBusy.earliest_reset`).
- `select_best()` becomes a thin wrapper: return `ranked[0].name`, or raise
  `AllAccountsBusy(earliest_reset)` when `ranked` is empty. **Behavior is identical**, so
  existing `server/tests` / selector tests stay green.

**Reason string** is derived from the dominant sort factor (no extra computation — the
keys already exist):
- Fresh account (`composite == 0`, no active window): `"freshest quota — saved for last"`.
- Otherwise lead with the discriminating factor, e.g.
  `"perishable quota, lowest load"`, `"lowest load"`, `"lowest session usage"`,
  `"lowest weekly usage"`. Keep each reason short (fits a tooltip).

### 2. Orchestrator — raw split counts

**File:** `server/sessions/orchestrator.py`

Add `live_session_counts() -> dict[str, dict[str, int]]` returning
`{account: {"active": int, "idle": int}}`. It reuses the **exact** liveness notion that
`live_session_load()` already uses (`_drivers` ∩ `_records`, `_ACTIVE_STATUSES` for
active vs idle) and the same snapshot-before-iterate guard. Accounts with no live
session are absent (rendered as zero by the UI).

This sits beside `live_session_load()` rather than replacing it: `live_session_load()`
returns weighted floats for the selector; `live_session_counts()` returns raw integers
for display. Both read the same two dicts so they cannot disagree about liveness.

### 3. Accounts API response

**File:** `server/api/accounts.py`

`_account_response(...)` gains three fields:

```jsonc
{
  "name": "tony@kindash.com",
  "status": "ok",
  "usage": { "session": {...}, "weekly": {...} },
  "nickname": null,
  "liveSessions": { "active": 2, "idle": 1 },  // from live_session_counts()
  "rank": 1,                                    // null when unavailable/at-ceiling
  "rankReason": "perishable quota, lowest load" // null when rank is null
}
```

`get_accounts()` is restructured to:
1. Collect `usages = {name: await orch.usage(name)}` for all accounts (as today, but
   into a dict instead of building responses inline).
2. Read `load = orch.live_session_load()`, `counts = orch.live_session_counts()`, the
   ledger `reserved` set (same source `pick_account` uses), and `cfg`/`now`.
3. Call `rank_accounts(usages, reserved, cfg, now, load=load)` once.
4. Build each response from the account info + its rank/reason (looked up by name) +
   its counts.

`POST /{name}/refresh` returns a single account; it has no full-fleet context to rank
against, so it returns `rank: null, rankReason: null` and the account's own
`liveSessions`. (Rank is a fleet-relative property; the periodic `GET /accounts`
refresh — every 30s — keeps ranks current.)

### 4. Remote mode

Live counts and load are daemon-side; the accounts response may cross the daemon→edge
shim (cf. the `pr_for` plumbing in #186 / #186-style asymmetry). The implementation must:
- Expose `live_session_counts()` on whatever orchestrator adapter the edge talks to
  (`InProcessOrchestratorAdapter` and the remote adapter), and
- Verify `GET /accounts` populates `liveSessions`/`rank`/`rankReason` in **both** the
  in-process and remote paths, with a test covering the remote shim.

This is called out explicitly because remote-mode path asymmetry has bitten this
project before (under-tested `RemoteOrchestrator`/`RemoteDriver` shims).

### 5. Frontend

**Types** (`web/src/api/types.ts`) — extend `Account`:

```ts
export interface LiveSessionCounts { active: number; idle: number }

export interface Account {
  name: string
  status: AccountStatus
  usage: { session: UsageWindow; weekly: UsageWindow } | null
  nickname?: string | null
  liveSessions: LiveSessionCounts        // new
  rank: number | null                    // new — 1-based, null when unavailable
  rankReason: string | null              // new
}
```

**Row UI** (`web/src/components/AccountsManager.tsx`, `AccountRow`):
- **Sessions chip:** render `2 active · 1 idle` when there is load; collapse to just
  the non-zero part (`2 active`, `1 idle`); render a muted `—` when both are zero.
- **Rank badge:** `#1 next` for rank 1, `#2` / `#3` for the rest; a muted `unavailable`
  when `rank == null`. The badge carries `title={account.rankReason ?? undefined}` so
  hovering shows the reason — native tooltip, no new dependency. (A richer Radix tooltip
  is a possible follow-up but out of scope here.)
- List order is unchanged. The badge lives in the same right-aligned action/metric row
  as the status pill and usage badges.

## Data flow

```
orchestrator (_drivers/_records) ──live_session_counts()──┐
ledger (usage, reservations) ─────────────────────────────┤
config + now ─────────────────────────────────────────────┤
                                                           ▼
                                       rank_accounts() in selector.py
                                                           │
                                          GET /api/accounts response
                                          (+ liveSessions, rank, rankReason)
                                                           │
                                       useAccounts() → AccountRow
                                       (sessions chip + rank badge + title tooltip)
```

## Error handling / edge cases

- **Usage fetch failed for an account** (`usage is None`): the account cannot be ranked
  (the selector skips `None` usages). It is treated as unavailable → `rank: null`,
  `rankReason: null`, and still shows its `liveSessions`. Status pill already conveys the
  problem.
- **All accounts at ceiling:** every account gets `rank: null`; nothing is marked
  `#1 next`. UI shows all as `unavailable`. (Mirrors `AllAccountsBusy` at selection time.)
- **Zero live sessions:** chip shows muted `—`.
- **Ties:** broken deterministically by the existing final sort key (account name), so
  rank numbers are stable across refreshes when inputs are equal.

## Testing

- **pytest — selector:** `rank_accounts` ordering matches the prior `select_best`
  choice; reason strings for each dominant factor; fresh-account "saved for last";
  unavailable list + earliest-reset parity; `select_best` still returns `ranked[0]` and
  raises `AllAccountsBusy` when empty.
- **pytest — orchestrator:** `live_session_counts()` splits active vs idle correctly and
  omits accounts with no live session (mirror existing `live_session_load` tests).
- **pytest — API:** `GET /accounts` includes `liveSessions`/`rank`/`rankReason` with the
  expected ordering; `/refresh` returns `rank: null`; the remote-shim path populates the
  fields.
- **vitest — web:** `AccountRow` renders the sessions chip (both/one/zero cases), the
  rank badge (`#1 next`, `#2`, `unavailable`), and the `title` reason.
- **Gates:** `tsc -b` (the real type gate), `eslint` (max-warnings 0), backend `ruff` +
  `mypy`.

## Files touched

- `src/ccswitch/selector.py` — `rank_accounts()` + `select_best()` rewrite.
- `server/sessions/orchestrator.py` — `live_session_counts()`; adapter exposure.
- `server/api/accounts.py` — restructured `get_accounts()`, extended `_account_response`.
- Remote orchestrator adapter — expose `live_session_counts()`.
- `web/src/api/types.ts` — `Account` fields + `LiveSessionCounts`.
- `web/src/components/AccountsManager.tsx` — sessions chip + rank badge in `AccountRow`.
- Tests across `server/tests/` and `web/src/**/__tests__` (or co-located).
