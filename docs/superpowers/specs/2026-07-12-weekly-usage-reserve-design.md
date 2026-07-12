# Per-Account Weekly Usage Reserve — Design

**Date:** 2026-07-12
**Status:** Approved

## Problem

Auto-rotation currently uses an account until it hits the hard ceiling (100% of a usage
window). The user wants to protect headroom on specific accounts: e.g. once
`tonystein85_gmail.com` reaches 90% of its weekly quota, ccswitch should stop picking it
for new sessions, keeping the last 10% in reserve for manual/interactive use.

## Decisions (user-confirmed)

- **Soft fallback:** the reserve blocks normal rotation, but if *every* account is at or
  over its reserve, ccswitch still picks one (the least weekly-used) rather than refusing
  to start sessions. The reserve alone can never cause `AllAccountsBusy`.
- **Per-account only:** each account carries its own optional threshold. Unset means no
  reserve — current behavior (excluded only at the 100% hard ceiling). No global default.
- **New picks only:** crossing the reserve does not touch already-running sessions and
  does not trigger proactive mid-session switching (the existing `soft_threshold`
  launcher behavior is unchanged).
- **Storage in the ledger:** `weeklyReservePct` lives per account in
  `~/.ccswitch/store/ledger.json` alongside `nickname`, so the core selector enforces it
  for both CLI and web flows. No server-DB account table.

## Design

### 1. Ledger (`src/ccswitch/ledger.py`)

- New per-account field `weeklyReservePct: float` (absent = no reserve).
- `set_weekly_reserve(name: str, pct: float | None)` — mirrors `set_nickname`; `None`
  clears the field.
- `weekly_reserves() -> dict[str, float]` — reader used by selection callers.
- `list_accounts()` entries include `weeklyReservePct` when set.

### 2. Selector (`src/ccswitch/selector.py`)

`rank_accounts()` gains `reserves: dict[str, float] | None = None` (pct 0–100 per
account name). For an account with usage:

- `weekly_pct >= hard ceiling` → excluded entirely (unchanged).
- `weekly_pct >= reserve` (and below the hard ceiling) → **demoted** to a new
  last-resort tier instead of ranked normally.

Final ranked order:

1. Normal candidates — existing perishability/load composite (unchanged).
2. Optimistic tier — usage unreadable, opt-in callers only (unchanged).
3. **Reserve-blocked tier** — ordered by lowest `weekly_pct` (least-used first),
   `reason="over weekly reserve — fallback"`.

Reserve-blocked accounts stay in the ranked list, so `select_picked` /
`AllAccountsBusy` semantics are untouched. Both selection callers pass reserves from
the ledger:

- `pick_account_detailed()` (`src/ccswitch/accounts.py`) — real selection.
- `GET /api/accounts` display ranking (`server/api/accounts.py`) — so the dashboard
  rank/reason matches what selection would actually do.

Copilot accounts need no special handling: their monthly premium-request quota is
already projected onto `weekly_pct` by `usage_from_proxy`, so a reserve applies to it
identically.

### 3. API (`server/api/accounts.py`)

- `PATCH /api/accounts/{name}`: body becomes
  `{ nickname?: str | null, weeklyReservePct?: float | null }`.
  - Validation: `1 <= pct <= 100` (422 otherwise).
  - Explicit `null` clears the reserve; field absent leaves it untouched
    (`model_fields_set` distinguishes the two, so nickname-only patches don't clobber
    the reserve and vice versa).
- `GET /api/accounts` and single-account responses (`_account_response`) include
  `weeklyReservePct` (null when unset).

### 4. Web UI (`web/src/`)

- `Account` type gains `weeklyReservePct?: number | null`.
- `AccountsManager.tsx`:
  - Reserve editor alongside the existing nickname editing — numeric input (1–100),
    clearable, saved via the PATCH hook.
  - Tick mark on the weekly usage bar at the reserve position.
  - "Over reserve" indicator when `weekly_pct >= reserve`; the rank-reason tooltip
    carries the selector's fallback reason string.

## Error handling

- PATCH with out-of-range pct → 422 with a clear message; unknown account → 404
  (existing `_check_name_and_slot`).
- Missing/corrupt ledger field: readers treat anything non-numeric as unset.
- Usage unreadable (`u is None`): reserve cannot be evaluated — the account follows the
  existing optimistic/excluded path, unchanged.

## Testing

- **Selector:** demotion at exactly the threshold (>= semantics); fallback tier ordered
  by least weekly usage; normal candidates always outrank reserve-blocked; optimistic
  tier ranks above reserve-blocked; hard ceiling still excludes outright; no reserves →
  behavior identical to today; reason strings.
- **Ledger:** set/clear/read round-trip; `list_accounts` exposure; absent field.
- **API:** PATCH sets/clears/validates; nickname-only PATCH preserves reserve (and
  reserve-only PATCH preserves nickname); GET exposes the field; 404/422 paths.
- **Web:** editor renders, saves, clears; over-reserve indicator; usage-bar marker.

## Out of scope

- Mid-session proactive switching on weekly reserve.
- Global default reserve.
- Session-window (5-hour) reserves — weekly only.
- Terminating or draining running sessions on an over-reserve account.
