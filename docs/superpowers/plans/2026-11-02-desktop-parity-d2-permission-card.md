# Desktop-parity D-2: permission card and plan comments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the permission card Desktop's four choices (Allow once · Allow for this session · Always allow… ·
Deny, with ⌘/Ctrl+. to deny), and let "Keep planning" on a plan approval send the user's comments back to Claude.

**Architecture:**
- "Allow for this session" is the existing `always` decision. The server already turns it into a `scope=session`
  rule from the first suggestion, or into the engine's own session accept on Codex (see
  `server/sessions/session_actions.py:318-330`). So that button is UI-only.
- Plan comments add an optional `message` to the permission decision, honoured only with `deny`. It rides the
  registry's existing per-request deny-reason mechanism (`PermissionRegistry.deny_all` / `_deny_reasons`,
  `server/sessions/permissions.py:~433`) into the model-visible `PermissionResultDeny(message=…)`.

**Tech Stack:** React + TS (vitest), FastAPI + pytest.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §6 (feedback) and §11 row D-2. Decisions:
`docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D11, D17.

## Global Constraints

- Button order and exact labels on a non-plan card: **Allow once** · **Allow for this session** · **Always allow…** ·
  **Deny**. Allow once keeps the filled primary weight. Always allow… keeps the quiet tertiary weight (the 2026-09-29
  critique: the broadest grant never wears the heaviest button).
- "Allow for this session" sends the existing `always` decision. Never add a second grant mechanism.
- "Always allow…" appears only when the standing-rule chooser is available (rules wiring present and the server sent
  suggestions). Its chooser opens with scope **This repo** when the session has a repo, and **This session**
  otherwise.
- Cards that forbid standing grants (`alwaysAllowed === false`, e.g. destructive app actions) show only Allow once
  and Deny.
- ⌘. / Ctrl+. denies the **oldest pending** permission card in the open session. A plan card is not a permission to
  deny, so it is skipped. The key must not fire in kiosk mode or while an overlay is open.
- The plan comment is optional: "Keep planning" with an empty comment behaves exactly as today. A non-empty comment
  is sent as `message` with `deny`. `message` on `allow` or `always` is rejected with 400 `MESSAGE_REQUIRES_DENY`.
  Trim it, and cap it at 4000 characters; longer gets 400 `MESSAGE_TOO_LONG`.
- Engines whose permission path cannot carry a deny message (check Codex in `server/codex/permissions.py`) still
  deny; the comment is then delivered as the next user prompt. Treat this fallback as part of the contract and test
  it.
- Gates: web typecheck, lint `--max-warnings 0`, full vitest. Server: ruff, mypy, focused pytest.

## Review Focus

1. **A comment on a card that was already answered elsewhere** (another device, or a restart): this must return the
   existing 404/"no longer pending" handling, not crash or send a prompt. Pinned in Task 1.
2. **Ctrl+. typed while composing in the session composer:** it still denies, since a chord is not a character, but
   only when a permission card is pending. With none pending it does nothing and does not swallow the key. Pinned in
   Task 3.
3. **A Codex session's "Allow for this session":** it must reach the engine's session accept, not create a rule.
   This is already server behaviour; pinned by a web test asserting `onDecision(permId, 'always')` and an existing
   server test (cite it).
4. **Keyboard digit order** after reordering the buttons: 1–4 map to the new visual order. Pinned in Task 2.
5. **A plan comment with only whitespace:** it is treated as no comment. Pinned in Task 1 (server) and Task 3 (web).

---

### Task 1: Server — optional deny message on a permission decision

**Files:**
- Modify: `server/api/sessions.py`: `PermissionResponseRequest` gains `message: str | None = None`, passed to
  `session_actions.resolve_permission(..., message=...)`.
- Modify: the WS permission-response frame handler that calls `resolve_permission`
  (`grep -n "resolve_permission" server/api/ws.py server/sessions/*.py`), accepting an optional `message` field.
- Modify: `server/sessions/session_actions.py` `resolve_permission`: validate (trim; empty becomes `None`;
  `message` with a decision other than `deny` gets `ActionError` 400 `MESSAGE_REQUIRES_DENY`; over 4000 characters
  gets 400 `MESSAGE_TOO_LONG`), then thread it through the orchestrator.
- Modify: the orchestrator's permission-resolution path, `RemoteOrchestrator`/fleet RPC (forward `message` only when
  set, so older daemons are unaffected) and `PermissionRegistry`: a deny carrying a message records it as that
  perm's deny reason, so `can_use_tool` returns `PermissionResultDeny(message=<comment>)`.
- Codex: if its approval path cannot carry text, deny and then enqueue the comment as the next user prompt through
  the existing prompt path, with the source marked as user.
- Test: `server/tests/test_permission_deny_message.py`

**Interfaces:**
- Produces:
  - `POST /sessions/{id}/permissions/{perm_id}` with body `{"decision": "deny", "message": "…"}` (use the route's
    real path).
  - The WS frame accepts `message`.
  - `resolve_permission(orch, db, *, session_id, perm_id, decision, identity, via=None, message: str | None = None) -> dict`

- [ ] **Step 1: Write failing tests**
  - **Claude engine:** deny + `"use sqlite instead"` makes the awaiting `can_use_tool` resolve to a
    `PermissionResultDeny` whose message contains `use sqlite instead`.
  - **Whitespace:** a whitespace-only message gives the same result as no message (the generic deny text).
  - **Validation:** `allow` + message gives 400 `MESSAGE_REQUIRES_DENY`; a 4001-character message gives 400
    `MESSAGE_TOO_LONG`.
  - **Not pending:** a non-pending perm with a message keeps today's 404 behaviour.
  - **Remote:** the proxy omits `message` when it is None.
  - **Codex:** deny + message ends with the deny applied and the comment enqueued as a user prompt.
- [ ] **Step 2: Run** `pytest server/tests/test_permission_deny_message.py` (scrubbed env per repo notes). Expected:
  FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus `pytest server/tests -k "permission or rule"`; `ruff check src/ server/`;
  mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(permissions): optional deny message on a permission decision (D-2)`

### Task 2: Permission card — four choices

**Files:**
- Modify: `web/src/features/sessions/panels/PermissionCard.tsx` (non-plan branch) and `AlwaysAllowDialog.tsx`
  (initial scope prop: `initialScope: RuleScope`).
- Test: `PermissionCard.test.tsx`, `AlwaysAllowDialog.test.tsx`; update `web/e2e` specs asserting the old labels
  (`grep -rn "Always allow (this session)\|Always allow…" web/e2e web/src`).

**Interfaces:**
- Consumes: the existing `onDecision(permId, 'allow' | 'deny' | 'always')`, `onPreviewRule`, `onGrantRule`.
- Produces: no new props on PermissionCard except what the dialog's `initialScope` needs.

- [ ] **Step 1: Write failing tests**
  - **Rules available:** the buttons in order are Allow once, Allow for this session, Always allow…, Deny.
  - **Session button:** Allow for this session calls `onDecision(permId,'always')`, with no chooser.
  - **Chooser scope:** Always allow… opens the chooser with scope This repo when `hasRepo`, This session when not.
  - **No rules wiring or no suggestions:** three buttons (no Always allow…).
  - **`alwaysAllowed === false`:** only Allow once and Deny.
  - **Digits:** 1–4 activate in the visual order.
  - **`A`:** still opens the chooser.
- [ ] **Step 2: Run** `(cd web && npx vitest run src/features/sessions/panels)`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): Desktop four-choice permission card (D-2)`

### Task 3: Plan comments and ⌘/Ctrl+. deny

**Files:**
- Modify: the plan branch of `PermissionCard.tsx`. Keep planning opens an inline, optional comment field (label
  "What should change?"). Its buttons are **Send & keep planning**, which sends `deny` with the message (or none
  when blank), and **Cancel**, which closes the field without answering. Esc inside the field cancels.
  Approve & run is unchanged.
- Modify: `web/src/session/useSessionRuntime.ts`: `onDecision(permId, decision, message?)`, sending `message` on the
  WS frame or HTTP call the runtime already uses.
- Modify: `web/src/shell/shortcuts.ts`: row `insession.denyPermission`, keys `Ctrl+.` (`keysMac` `⌘.`), match
  `cmdOrCtrl(e.key === '.')`, `when: c.sessionOpen && c.interactive && !c.overlayOpen && c.hasOpenPermission`. Add
  `hasOpenPermission` to `ShortcutContext`, true only for a non-plan permission card. Wire the handler through
  `shell/sessionGestures.tsx` facts (`denyOldestPermission()` from `MainPaneSession`, alongside `onStop`).
- Test: `PermissionCard.test.tsx`, `useShortcuts.test.tsx`, `useSessionRuntime` test.

- [ ] **Step 1: Write failing tests**
  - **Comment sent:** Keep planning, type "split step 3", then Send & keep planning calls
    `onDecision(permId,'deny','split step 3')`.
  - **Blank comment:** Send with a blank or whitespace field calls `onDecision(permId,'deny')`, with no message.
  - **Cancel:** Cancel or Esc in the field sends nothing.
  - **Ctrl+. deny:** with two permission cards pending, Ctrl+. denies the oldest only, including while the composer is
    focused.
  - **Ctrl+. no-op:** with no card pending, Ctrl+. does nothing and doesn't call `preventDefault`. With only a plan
    card pending it does nothing.
  - **Kiosk:** Ctrl+. does nothing.
  - **Runtime:** the message reaches the request body or frame.
- [ ] **Step 2: Run** the focused vitest files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): plan approval comments and Ctrl/⌘+. deny (D-2)`

### Task 4: Benchmark and docs

- Modify: `scripts/desktop_parity_benchmark.py`:
  - `Permission card: once / this session / always / deny` → `Y`.
  - `Plan approval with comments` → `Y`, with note `overall comment; per-step comments reuse D-5 line comments`.
- Modify: re-generate `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` §3 from the script output. Update
  `docs/product/features/session-permissions.md` (or the doc that describes the card buttons and plan approval;
  grep for "Always allow") and the spec §11 D-5 row to note that per-step plan comments land with line comments.
- [ ] **Step 1: Run** `python3 scripts/desktop_parity_benchmark.py`. Expected: Target completion ≥ 69%.
- [ ] **Step 2: Paste** the output and update the docs. Commit `docs: D-2 benchmark rerun and permission-card docs`
