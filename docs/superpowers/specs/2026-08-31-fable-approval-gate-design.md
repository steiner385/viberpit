# Model-Approval Gate — Design Spec

**Date:** 2026-08-31
**Status:** Approved design, pending implementation plan
**Feature:** Hold sessions pending human approval when a gated model (default: fable) is selected.

## Problem

Sessions must never *default* to fable, and any selection of fable — at session
launch or mid-session — must **require human approval**. Because ccswitch runs
sessions unattended (automations create sessions in full-auto), the gate cannot
be a synchronous "confirm in the dialog" prompt: when no human is present the
request must be **held pending** until a human resolves it, not silently denied
or silently downgraded to another model.

The mechanism must be configurable: which models are gated (a denylist), what
action a gated model triggers, and per-account overrides.

## Decisions (locked)

- **Scope:** a configurable **denylist** of gated models (not a fable-only
  hardcode). Fable is the seeded default entry.
- **No-human path:** **hold pending approval** — the request is parked, not
  denied, until a human resolves it.
- **Approval UX:** **async pending-approval panel** (mirrors the in-session
  permission-block pattern), plus a dashboard affordance for launch-time holds.
- **Config surface:** **env → global (`AppSettings`) → per-account** hierarchy,
  mirroring `resolve_permission_mode`.
- **Deny at launch:** held session is closed via the **existing terminal-close
  path** (the status a cancelled/closed session already uses) with a
  `model_approval.deny` audit reason — *not* a new `denied` status value, to keep
  `pending_approval` the only new status the codebase must thread. No silent
  fallback to a different model than the one requested.
- **Per-account override:** adjusts the *action* in both directions — an account
  may `allow`, `require_approval`, or `block` an otherwise-gated model.

## Chosen approach (Approach A)

Represent "held" as a **session lifecycle state**, reusing the machinery that
already carries `queued` sessions, and reuse the permission-card pattern for the
mid-session panel. Rejected alternative: a generic `ApprovalRequest` table
(Approach B) — it would still require the launch-time "held" session state, so it
duplicates the carrying logic for a single gated dimension today (YAGNI /
constitution VI). Approach B remains the documented extension point if approvals
proliferate (spend, dangerous accounts): refactor Approach A's session-scoped
fields into that table then.

## Design

### 1. Policy resolver (single chokepoint)

New module `server/sessions/model_policy.py`, parallel to
`resolve_permission_mode()`:

```
resolve_model_policy(db, *, model, account) -> "allow" | "require_approval" | "block"
```

- Returns `allow` immediately when `model is None` or its **tier** is not in the
  denylist.
- **Denylist** (which models are gated): env `CCSWITCH_MODEL_APPROVAL_DENYLIST`
  (default `fable`) → `AppSettings.model_approval_denylist`. Matching is by
  **tier**, not literal string, via a `model_tier(model)` helper so `fable`,
  `fable[1m]`, and pinned `claude-fable-5` all match the single denylist entry
  `fable`.
- **Action** for a gated model: per-account ledger pin → `AppSettings`
  `model_approval_action` → env `CCSWITCH_MODEL_APPROVAL_ACTION` (default
  `require_approval`). Precedence mirrors `resolve_permission_mode`.

**Single source of truth for tiers:** `model_tier()` lives in
`server/sessions/model_catalog.py` (the existing model SoT) and is the server
mirror of `web/src/session/modelName.ts`'s `TIERS`. Both the catalog and the
policy resolver share it — no parallel tier logic.

### 2. Gate points — orchestrator, not the HTTP route

The gate lives in **`orchestrator.create_session()`** and
**`orchestrator.set_model()`**, the common chokepoint for both the API and
automations (automations call the orchestrator directly; gating only the route
would let unattended callers bypass it).

- **Launch (`create_session`)**: after the model is resolved, call
  `resolve_model_policy`.
  - `block` → raise / reject.
  - `require_approval` → persist the record with status `pending_approval`,
    `pending_model=<model>`, `pending_model_requested_by=<identity>`,
    `pending_model_requested_at=<now>`; **do not spawn**.
  - `allow` → today's spawn/enqueue path.
- **Mid-session (`set_model`)**:
  - `require_approval` → stash `pending_model` + requester on the running record,
    publish an event; the session keeps running on its current model.
  - `block` → reject.
  - `allow` → apply now.
- **Rotation is exempt by construction**: `_reassign()` re-applies an
  already-approved model via the low-level `driver.set_model()`
  (orchestrator.py:7668), which is *below* the gated orchestrator method. A test
  pins that rotation never re-triggers approval.

### 3. Data model

- Session **status** gains one value: `pending_approval` (status is a string
  column — no schema change for the value itself).
- Three **nullable** columns on the session record (auto-applied by
  `_reconcile_columns` on both SQLite and Postgres):
  - `pending_model TEXT NULL`
  - `pending_model_requested_by TEXT NULL`
  - `pending_model_requested_at TIMESTAMP NULL`
- Two **nullable** columns on `AppSettings` (null → fall through to env):
  - `model_approval_action TEXT NULL`
  - `model_approval_denylist TEXT NULL`
- Add an Alembic revision chained off the newest one as the reviewable record
  (reconcile is what actually applies it at runtime). **No new model**, so the
  Postgres-compat lists (`_REQUIRED_EXTRAS`, `RECONCILE_EXEMPT`) do **not**
  change — all additions are nullable columns.

**Capacity + status threading (main risk):** `pending_approval` sessions must be
**excluded from active-slot counting** so a parked automation session does not
hold a slot hostage. The plan must enumerate and update every status consumer:
active-slot/capacity counting, dashboard filters, fleet host ownership, session
list serialization, and the web reducer.

### 4. Resolution API + WebSocket

- **REST** `POST /api/sessions/{id}/model-approval` `{decision: "approve" | "deny"}`
  (REST, not WS, because launch-time holds are resolved from the dashboard, which
  has no per-session socket open).
  - **Approve**: launch-time → normal spawn/enqueue path; mid-session → apply
    `driver.set_model(pending_model)`. Clear pending fields. Audit
    `model_approval.approve` with approver identity.
  - **Deny**: launch-time → close via the existing terminal-close status (the one
    a cancelled/closed session already uses); mid-session → clear pending, keep
    current model. Audit `model_approval.deny`. The exact status constant is
    identified from the code during planning.
- `model_approval.requested` audit event on gate entry.
- **Settings API** (`/api/settings`) gains `modelApprovalAction` +
  `modelApprovalDenylist`. **Account-defaults API** gains per-account
  `modelApprovalAction`.

### 5. Frontend

- **Mid-session**: new `PendingModelApprovalPanel` (sticky, one-at-a-time)
  mirroring `PendingPermissionPanel`, shown when the session has `pending_model`
  set. Approve / Deny → the REST endpoint.
- **Launch-time**: the dashboard session card renders a `pending_approval` state
  with inline Approve / Deny.
- **New Session dialog**: when the picked model's tier is gated, show an inline
  note ("Fable requires approval — this session will be held until approved"),
  reusing `modelName.ts` `TIERS` for detection. Add a guard + test that the
  picker's **default selection is never a gated tier** (directly satisfies "don't
  default to fable").
- **Settings UI**: global action + denylist controls; per-account action override
  in account settings.

### 6. Config

- `server/config.py`: `CCSWITCH_MODEL_APPROVAL_ACTION` (default
  `require_approval`), `CCSWITCH_MODEL_APPROVAL_DENYLIST` (default `fable`).
- `AppSettings` columns override env; per-account ledger `modelApprovalAction`
  overrides global.

## Error handling

- `block` action → `create_session`/`set_model` raise a typed error surfaced as a
  4xx on the API path; automations receive the same exception and log it.
- Approving an already-resolved or non-pending session → idempotent no-op / 409,
  audited.
- Deny of a launch-time hold is terminal; the requester must start a new session
  to pick a different model (no silent model substitution).
- Daemon restart: launch-time holds persist via session status in the DB;
  mid-session pending persists via `pending_model` on the record, so both
  re-surface after restart / adoption.

## Testing

All four `lint-and-test` steps (`ruff`, `mypy`, core `pytest tests/`, `pytest
server/tests/`) plus web (`vitest`, `tsc --noEmit`, `eslint`).

- **Policy resolver**: precedence (account → global → env); tier matching
  (`fable`, `fable[1m]`, `claude-fable-5` gated; `opus`/`sonnet`/`None` pass).
- **Orchestrator**: gated `create_session` → `pending_approval`, no spawn, no slot
  consumed; approve → spawns; deny → existing terminal-close status. Gated `set_model` →
  pending; approve applies; deny clears. **Rotation re-apply does not trigger the
  gate.**
- **API**: `model-approval` auth + audit; settings + account-defaults round-trip.
- **Web**: reducer handles `pending_model` + `pending_approval`; both panels
  render; New-Session note + never-default-to-fable guard; settings controls.
- **Schema**: new nullable columns reconcile clean on both dialects; drift assert
  green; Alembic revision added.

## Out of scope

- Gating the *config action* of setting an account's `defaultModel` to fable —
  gating is at point-of-use (session creation), so an account defaulted to fable
  simply hits the approval gate on every new session.
- Intercepting the binary's own `default`-alias resolution if it ever resolved to
  fable server-side (ccswitch passes `model=None` and cannot see the binary's
  choice pre-launch). Noted; not addressed.
- A generic multi-kind approval subsystem (Approach B) — documented extension
  point only.
