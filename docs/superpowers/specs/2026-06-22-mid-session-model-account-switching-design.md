# Mid-Session Model & Account Switching — Design

**Date:** 2026-06-22
**Feature:** Let a user change both the **model** and the **account** of an individual live session, mid-session, from the web UI and via slash commands.
**Status:** Design — approved for planning.

## Problem

Today a session's account is chosen once at creation and only changes **automatically** —
either when the account hits its usage ceiling (`_rotation_monitor`) or when a turn fails with
429/401 (failure-driven recovery, merged in #131). The model can be changed mid-session via the
`/model` command, but that change is **ephemeral**: there is no `model` column on the `Session`
row and no `model` field on `SessionRecord`, so it is lost on rehydrate and on any driver rebuild
(rotation/recovery). The user has no way to **manually** move a session to a chosen account, and no
first-class UI for either axis.

This feature adds manual, user-driven control of both axes while reusing the existing switch
machinery rather than duplicating it.

## Existing source of truth (route through, do not duplicate)

The merge in #131 already centralized the "rebuild this session's driver on a new account,
resuming the same Claude conversation" sequence into a single primitive:

- **`orchestrator._reassign(record, new_account, *, reason)`** (`server/sessions/orchestrator.py`
  ~L1171–1217): interrupt old driver → close → compose new `config_dir` → trust project dir →
  build new `SessionDriver` with `resume=claude_sid` and the same callbacks → start → register →
  update `record.account` → replay last prompt → persist `account_name` to DB → publish a
  `rotated` event.
- All current callers funnel through it:
  - usage ceiling: `_rotation_monitor` → `_do_rotate` → `_rotate_away` → `_reassign`
  - 429 (>30s): `_recover_session` → `_rotate_away` → `_reassign`
  - 401 (recompose / then rotate): `_recover_session` → `_recompose_same_account` / `_rotate_away`
    → `_reassign`

The model string is an opaque pass-through to the `claude` binary: `driver.set_model(model)` →
SDK control request `{"subtype": "set_model", "model": model}`. The `claude-agent-sdk` exposes
**no** model catalog; the authoritative list lives in the `claude` binary's `/model` picker
(aliases: `default`, `opus`, `sonnet`, `haiku`, `opusplan`; plus pinned IDs like
`claude-haiku-4-5`). Max-account authorization is enforced by the binary/account, not by ccswitch.

Account list for the UI already exists: `GET /api/accounts` (`server/api/accounts.py`), already
consumed by the web dashboard. `limited` is already a real session status (`server/sessions/
types.py`, rendered as "Usage limit" in `web/src/session/statusTier.ts`).

## Design

### 1. Manual account switch — a thin caller of `_reassign`

Add `orchestrator.switch_account_manual(session_id, new_account)`:

1. Validate `new_account` is a known account (reuse the same source `select_account` draws from).
2. Set the session's pin: `record.account_pinned = True`, persist.
3. Call `await self._reassign(record, new_account, reason="manual")`.

No refactor of `_reassign` / `_do_rotate` is required — this is one more caller. The conversation,
`claude_session_id`, and transcript survive via the existing `resume=claude_sid` path.

### 2. Model persistence + re-apply on every driver rebuild

- Add nullable `Session.model: str | None` and mirror `SessionRecord.model`.
- Route `set_model` through the orchestrator so it **persists** to DB + record (today it only calls
  `driver.set_model`, which is ephemeral).
- **Inside `_reassign`**, after the new driver starts, re-apply `record.model` (when set) via
  `new_driver.set_model(record.model)`. Because every rotation/recovery path goes through
  `_reassign`, the chosen model now survives usage rotation **and** 429/401 recovery — not just
  manual switches.

### 3. Pinning semantics

Add nullable `Session.account_pinned: bool | None` (mirrored on `SessionRecord`); `None`/`False`
both mean "auto-rotate eligible". Two axes are deliberately separated:

- **Usage-ceiling rotation (`_rotation_monitor`)** — *busy*. A pinned session is **skipped**.
  When a pinned account reaches 100%, the session is set to the existing `limited` status and a
  lifecycle event is emitted; it is **not** moved.
- **Failure-driven recovery (429/401, `_recover_session`)** — *broken*. A pinned session **is still
  recovered** (rotated to a healthy account) to avoid stranding a live session on a dead account.
  On such an override, recovery emits its event and clears `account_pinned` (the user's pin pointed
  at a now-unusable account); the UI reflects that the pin was overridden by recovery.
- A manual **model** change never pins the account (orthogonal axes).

### 4. Curated model list — single source of truth

- New module `server/sessions/model_catalog.py` exporting the stable alias list
  `["default", "opus", "sonnet", "haiku", "opusplan"]` (verified against the real `claude` binary;
  aliases chosen over pinned IDs because pinned IDs churn per binary release).
- New `GET /api/models` returns the curated list so the **web has one source** — no hardcoded TS
  catalog. Free-text entry remains allowed for pinned IDs, preserving today's `/model <id>`
  capability.
- `/model` and a new `/account <name>` control command both validate against their catalogs
  (`/account` against the live accounts; `/model` accepts any catalog alias or free-text id).

### 5. Surfaces — both, sharing one path

- **Commands** (`server/sessions/commands/dispatcher.py`): extend `/model` (now persists); add
  `/account <name>` → `switch_account_manual`.
- **UI dropdowns** in `SessionMeta` / `StatusBar`:
  - Account selector — data from `GET /api/accounts` (with usage), shows current + pin state.
  - Model selector — data from `GET /api/models`, plus a free-text field for pinned IDs.
  - On select, the dropdowns dispatch the **same** WebSocket `command` messages
    (`{type: "command", name: "model"|"account", args}`) the slash commands produce — one code
    path, two entry points. No new REST mutation endpoints.
- **Events**:
  - Reuse the existing `Rotated` event (`server/sessions/events.py`) for account changes;
    `reason="manual"` distinguishes a user-initiated switch from auto/recovery.
  - Extend the session-meta payload (`EvSessionMeta`) to carry `model`, `account`, and
    `account_pinned` so the UI can render current state and the pin indicator.
  - Pinned-at-ceiling emits a `limited` lifecycle signal (status already exists).

### 6. Data-model migration note

Per the dev-DB rule (`create_all` never ALTERs existing tables; `init_db` backfills missing
**nullable** columns), both `model` and `account_pinned` are added **nullable**. No Alembic
migration needed for dev SQLite; production follows the standard migration path. Covered by the
existing `server/tests/test_schema_reconcile_coverage.py` invariant.

## Testing (TDD)

- `switch_account_manual`: resumes the same Claude conversation, persists `account`, sets
  `account_pinned=True`, emits `rotated` with `reason="manual"`.
- `_rotation_monitor` skips a pinned session and sets it `limited` at the ceiling (no rotation).
- Failure recovery **still** rotates a pinned session on 401/429 and clears the pin.
- Persisted `model` is re-applied to the new driver across a `_reassign` (assert `set_model` called
  with `record.model`).
- `/account` command dispatch; `/model` now persists to DB + record.
- `GET /api/models` returns the curated alias list; `model_catalog` is the only definition.
- Schema-reconcile coverage for the two new nullable columns.
- Web: account/model dropdowns dispatch the correct WS `command` payloads; pin indicator renders;
  meta updates on `rotated` / meta events.

## Out of scope

- Querying the Anthropic Models API (`GET /v1/models`) — ccswitch drives Max OAuth via the SDK, not
  an API key; the binary's `/model` contract is the authority.
- Per-account model authorization UI — enforcement stays with the binary/account; ccswitch offers
  the alias set and surfaces rejections.
- Changing how auto-rotation or failure recovery decide *which* account to pick (`select_account`,
  `RecoveryState` quarantine) — unchanged.
