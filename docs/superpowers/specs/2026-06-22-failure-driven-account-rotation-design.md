# Failure-driven account rotation for live sessions

**Date:** 2026-06-22
**Status:** Approved (design)
**Area:** `server/sessions/` (orchestrator, driver, translator), `server/usage/`, ledger

## Problem

A live `claude` session is bound to one account at launch via `CLAUDE_CONFIG_DIR`. When that
account starts returning **429 (rate-limited)** or **401 (invalid credentials)** during a turn, the
session simply fails the turn and surfaces the raw error to the UI ("Failed to authenticate. API
Error: 401 Invalid authentication credentials" / "API Error: 429 ..."). The user must manually
intervene.

Two real incidents motivated this:

1. **429 storm.** Several sessions stacked on one account (`kindash`) drove the usage endpoint to
   429 repeatedly; the account's metrics froze and turns failed.
2. **401 from a refresh-token race.** Multiple concurrent sessions sharing one *single-use* OAuth
   refresh token raced on refresh; losers had their live `.credentials.json` `refreshToken`
   clobbered to empty, so every turn returned `401 Invalid authentication credentials` — while the
   **store** copy of the credentials remained intact.

In both cases the ledger still reported the account `status: ok`, because `needs-reauth` only trips
on ccswitch's *own* `oauth.AuthError` during its refresh path — an in-session 401 or a usage-poll
429 never propagates. The dashboard showed a falsely healthy account whose sessions were dying.

## What already exists (reuse, don't rebuild)

`server/sessions/orchestrator.py` already implements live account switching:

- **`_do_rotate(record)`** — interrupts + closes the current driver, calls
  `select_account(exclude={old})`, `compose()`s the new account's config dir, launches a new
  `SessionDriver` with `resume=claude_session_id` (same `claude` conversation, new account), replays
  the last prompt, updates `DbSession.account_name`, and emits a `rotated` event.
- **`_rotation_monitor()`** — subscribes to the usage poller and calls `_do_rotate` **only** when
  `session_pct >= _ROTATION_CEILING` (currently `100.0`). A usage-poll 429 yields `usage = None`, so
  the monitor `continue`s — it never reacts to rate-limit or auth failures.
- **`select_account(exclude=...)`** (`server/sessions/selection.py`) — already supports excluding
  accounts; wraps `ccswitch.accounts.pick_account`, raises `AllAccountsBusy(earliest_reset=...)`.

So the work is **add the missing failure-driven triggers and guardrails**, reusing `_do_rotate`.

## Goal

When a live session's turn fails with a **rate-limit (429)** or **auth (401)** error, automatically
recover it — preferring same-account recovery, then rotating to a healthy account — with guardrails
against thrash, and stop the ledger from reporting a falsely-healthy account whose sessions are
failing.

Out of scope: rotating on a *usage-poll* 429 (too noisy — a throttled usage endpoint does not mean
inference is blocked); changing the existing `session_pct`-based proactive rotation.

## Design

### 1. Signal flow

```
turn fails (429 / 401) during a turn
  ├── SDK RateLimitEvent  ──┐   translator currently IGNORES this — start surfacing it
  └── _drain() exception  ──┤   driver.py:325 — classify exc -> kind + retry_after
                            ▼
        driver emits ev.TurnFailure(kind, retry_after, account)
            + on_recoverable_failure(session_id, kind, retry_after)   (new callback, mirrors on_status)
                            ▼
        orchestrator._recover_session(record, kind, retry_after)
```

**Classification** (`server/sessions/failure.py`, new) maps a turn failure to:

- `rate_limit` — HTTP 429. Carries `retry_after: float | None` (parsed from the SDK
  `RateLimitEvent`, an `APIStatusError`-style attribute, or a `Retry-After` substring; capped like
  `oauth._RETRY_AFTER_CAP`).
- `auth_401` — HTTP 401 / "invalid authentication credentials".
- `other` — anything else. Preserves today's behavior: surface `ErrorEvent`, mark turn failed, **no
  rotation**.

Classification is type/attribute-first with a string fallback, so it is robust whether the SDK
raises a typed error or the driver only has the stringified message.

### 2. Recovery policies

Handled in `orchestrator._recover_session`:

- **429:**
  - If `retry_after` present and `<= RETRY_AFTER_MAX` (~30s): wait, then replay `_last_prompt` on the
    **same** driver (no rotation, no resume churn).
  - Else (long/absent `Retry-After`, **or** the same-account retry 429s again): **quarantine** the
    account and **rotate** to a different one.
- **401:**
  - `compose(account)` to rewrite the clobbered live `.credentials.json` from the (usually intact)
    store, then **reassign to the same account** (new driver, `resume=claude_sid`, replay last
    prompt). A running `claude` process holds the bad creds in memory, so a relaunch is required —
    rewriting the file alone is insufficient.
  - If the recompose-retry still 401s: **quarantine** the account, **mark it `needs-reauth`** in the
    ledger (the store creds are genuinely bad), and **rotate** to a different account.

Rotation and "recompose same account" share one generalized path:
`_do_rotate(record)` → `_reassign(record, new_account, *, recompose=True)`, where `new_account` may
equal the old account (the 401 recompose case). The existing usage-`pct` rotation calls
`_reassign(record, select_account(exclude=quarantined() | {old}))`.

### 3. Guardrails

- **Quarantine registry** — in-memory `dict[account -> quarantine_until: float]` owned by the
  orchestrator. No ledger migration (rate limits are transient; losing the map on restart is
  acceptable, and it avoids the dev-SQLite `create_all` no-ALTER constraint).
  - 429 cooldown = `Retry-After` if present else `QUARANTINE_429` (~60s).
  - recompose-failed 401 = `QUARANTINE_401` (~5min) + ledger `needs-reauth`.
  - `quarantined() -> frozenset[str]` returns accounts still in cooldown (expired entries pruned).
  - **Both** `_reassign` selection **and** new-session creation pass `exclude=quarantined()` to
    `select_account`, so a quarantined account is skipped everywhere, not just during rotation.
- **Per-session rotation cap** — `dict[session_id -> list[timestamp]]`. More than `MAX_ROTATIONS`
  (~3) within `ROTATION_WINDOW` (~5min) stops rotating that session and routes to *paused*.
- **Paused backstop** — when `select_account` raises `AllAccountsBusy`, or the per-session cap trips:
  set session status `paused`, emit a `paused` event carrying `earliest_reset`, and let the next
  poller tick (or a wake scheduled at `earliest_reset`) attempt recovery. No silent failure.

### 4. Observability fix (in scope)

The same classification that drives rotation also closes the "falsely-healthy ledger" gap:

- When a session turn fails with `auth_401` **and the recompose-retry also fails**, the account's
  store creds are actually bad — set the ledger entry to `needs-reauth` (reusing the existing flag
  and its existing UI treatment). This is the one place an in-session 401 is authoritative about the
  account, not just the session.
- A transient 429 does **not** change account status (consistent with
  `ccswitch-needs-reauth-mechanics`: transient HTTP must not trip the sticky flag). It is reflected
  only via the per-session `paused`/`rotated` events, not account status.
- Emit a `rotated` / `paused` event (already partly present) so the UI can show *why* a session
  moved or stalled, instead of a bare error string.

This keeps account status truthful (`needs-reauth` only for genuinely-bad creds) while making
session-level failures visible.

## Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|-----------|
| `failure.classify(exc \| event) -> TurnFailure` (new `failure.py`) | Pure: map an error/event to `{kind, retry_after}` | none (stdlib) |
| `translator` | Stop dropping `RateLimitEvent`; surface it as a typed event | sdk_contract |
| `driver` | On turn failure, classify and emit `TurnFailure` + invoke `on_recoverable_failure` | failure, events |
| `orchestrator._recover_session` | Apply 429/401 policy, drive `_reassign`, enforce guardrails | selection, configdir, ledger, quarantine |
| `orchestrator` quarantine registry + rotation cap | In-memory cooldown/cap state; `quarantined()` | none |
| ledger | `needs-reauth` set only on recompose-failed 401 | existing |

Each unit is independently testable: the classifier is pure; the quarantine registry is a small
state object; the orchestrator recovery is tested with a fake `sdk_factory`.

## Testing

- **Classifier** (`failure.py`): 429 with `Retry-After`, 429 without, 429 from `RateLimitEvent`, 401,
  generic exception → `other`.
- **Quarantine registry**: cooldown set/expiry; `quarantined()` excludes in-cooldown, prunes expired.
- **Per-session cap**: rotations counted within window; exceeding cap → `paused`.
- **Orchestrator recovery** (fake `sdk_factory`):
  - 429 with short `Retry-After` → same-account replay, no `account_name` change.
  - 429 with no `Retry-After` → `_reassign` to a new account; last prompt replayed; `DbSession.account_name` updated; `rotated` event emitted; old account quarantined.
  - 401 → `compose` called, same-account relaunch; on second 401 → rotate + ledger `needs-reauth`.
  - `AllAccountsBusy` → `paused` event with `earliest_reset`; no crash.
- **Observability**: recompose-failed 401 sets ledger `needs-reauth`; transient 429 does **not**.
- Regression: existing `_rotation_monitor` (usage-pct) path still rotates and now also respects
  quarantine excludes.

## Tunables (constants, one place)

`RETRY_AFTER_MAX` ~30s · `QUARANTINE_429` ~60s · `QUARANTINE_401` ~300s · `MAX_ROTATIONS` ~3 ·
`ROTATION_WINDOW` ~300s. Defined as module constants in the orchestrator alongside
`_ROTATION_CEILING`.

## Risks

- **All accounts rate-limited** → every rotation hits `AllAccountsBusy`; the per-session cap +
  paused backstop bound the thrash. Acceptable.
- **SDK error shape varies** → classifier is type-first with string fallback and is unit-tested
  against both shapes; an unrecognized error falls through to `other` (today's behavior), never a
  crash.
- **Recompose loop on 401** → bounded by the single recompose-retry before rotating, plus the
  per-session cap.
