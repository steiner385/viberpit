# Always-allow standing rules — PR 2 (card UI) + PR 3 (management) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The permission card's "Always allow…" opens a chooser that grants a standing rule (pattern + scope, with a live preview). Settings and the session view list, edit and revoke rules.

**Architecture:** This plan is web-only and builds on the PR 1 server API (spec §5).
- **Card.** The card keeps `onDecision` for allow and deny. A new runtime callback pair, `onPreviewRule` and `onGrantRule` (HTTP via `api` in `web/src/api/client.ts`), drives a presentational `AlwaysAllowDialog`. The server resolves the card, and the existing `permissionResolved` WS event clears it.
- **Management.** Management uses React Query hooks (`usePermissionRules`, `useUpdatePermissionRule`, `useRevokePermissionRule`) in `web/src/api/hooks.ts`. It has a new Settings section and a per-session readout in `SessionMeta`.

**Tech Stack:** React + Vite + TS, Tailwind, React Query, vitest + RTL, `web/src/ui` primitives (`Dialog`, `RadioGroup`, `Input`, `Button`, `useConfirm`).

**Spec:** `docs/superpowers/specs/2026-10-11-always-allow-permission-rules-design.md` (§5 API, §6 web).

## Global Constraints

- Web scripts run from `web/` in a subshell: `(cd web && npm run typecheck)`, `(cd web && npm run lint)`, `(cd web && npm run test)`, `(cd web && npm run build)`. **Never** a bare `cd web` (it breaks the repo's Bash hook for every later call). Run the **full** `npm run test`; do not run only the changed files.
- `web/node_modules` is a symlink to `/home/tony/GitHub/viberpit/web/node_modules`. Create it if missing: `ln -s /home/tony/GitHub/viberpit/web/node_modules web/node_modules`. Never commit it.
- Vitest piped through `| tail` masks the exit code. Use `(cd web && npm run test) > /tmp/ar-web-test.log 2>&1; echo EXIT=$?`.
- No new dependencies. Reuse `web/src/ui` primitives and the `api` client. Do not hand-roll fetch.
- The server is the single source of truth for matching, descriptions and risk. The web side never matches patterns.
- Copy: the buttons are **Allow once**, **Always allow…**, **Deny**. The scope labels are **This session**, **This repo**, **All sessions**.
- Commit with a message file (`git commit -F`) ending `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Grep `web/e2e` for every visible string you change, and update the specs that match.

### Server API (from PR 1)

| Call | Response |
|---|---|
| `POST /api/sessions/{sid}/permissions/{permId}/always` `{pattern, scope, confirm?}` | `200 {rule, resolvedPermIds}` |
| `POST /api/permission-rules/preview` `{sessionId, permId?, pattern, scope}` | `{valid, level, reasons, description, needsConfirmation, coversRequest, alsoPending}` |
| `GET /api/permission-rules[?sessionId=]` | `RuleWire[]` |
| `PATCH /api/permission-rules/{id}` `{pattern, confirm?}` | `RuleWire` |
| `DELETE /api/permission-rules/{id}` | 204 |

- `RuleWire` = `{id, profileId, pattern, scope, scopeKey, position, createdBy, createdVia, createdAt, hitCount, lastHitAt, description}`.
- Errors come back as `{detail: {code, message, ...}}`. The codes are:
  - `RULE_REFUSED` (400, with `reasons`)
  - `RULE_NEEDS_CONFIRMATION` (409, with `level` and `reasons`)
  - `PATTERN_DOES_NOT_COVER` (400)
  - `NO_REPO` (400)
  - `RULE_NOT_ALLOWED` (400)
  - `RULES_HUMAN_ONLY` (403)
  - `PERMISSION_NOT_PENDING` (404)
  - `DUPLICATE_RULE` (409)
  - `RULES_DISABLED` (404)
- The WS `permissionRequest` event carries `ruleSuggestions: [{pattern, description, level}]`.
- Confirm the exact shapes against `server/api/permission_rules.py` before coding. If they differ, the server wins: adapt the types, and note it in your report.

## Review Focus

1. **Double resolution.** A grant succeeds over HTTP, then the WS `permissionResolved` for the same permId arrives. The reducer must stay idempotent: no duplicate notice, no crash.
2. **Stale card.** The user opens the chooser and the card resolves elsewhere (another tab or a sweep). The dialog must close, or show "no longer pending" on `PERMISSION_NOT_PENDING`, never hang.
3. **Confirmation loop.** A 409 `RULE_NEEDS_CONFIRMATION` must show the reasons, then retry with `confirm: true` on an explicit click. It must never auto-confirm.
4. **Scratch sessions.** "This repo" is disabled when the session has no repo.
5. **Keyboard.** `A` opens the chooser only when focus is inside the card. Enter grants. Esc closes and returns focus to the card. The existing digit shortcuts keep working: 1 = Allow once, 2 = Deny, 3 = Always allow….

---

## PR 2: card UI (branch `feat/always-allow-card`, stacked on `feat/always-allow-rules`)

### Task W1: Event plumbing and runtime callbacks

**Files:**
- `web/src/session/events.ts`: add `ruleSuggestions?: RuleSuggestion[]` to `EvPermissionRequest`, and export `type RuleSuggestion = { pattern: string; description: string; level: 'ok' | 'warn' }`.
- `web/src/session/reducer.ts`: the permission `Block` gains `ruleSuggestions?`, and the `permissionRequest` case copies it in.
- `web/src/session/useSessionRuntime.ts`:
  - Add `onPreviewRule(permId, pattern, scope) => Promise<RulePreview>` and `onGrantRule(permId, pattern, scope, confirm) => Promise<GrantResult>`, both using `api.post`.
  - On grant success, dispatch `{ type: '_resolvePermission', permId, decision: 'always' }`.
  - Return both callbacks from the runtime.
- `web/src/api/types.ts`: add `RulePreview`, `RuleWire` and `GrantResult` types (the shapes above).
- Tests:
  - the reducer test: a `ruleSuggestions` round trip; `_resolvePermission` followed by a WS `permissionResolved` for the same permId leaves exactly one notice;
  - a runtime test (find the existing `useSessionRuntime` test file) for `onGrantRule` dispatching on success and not dispatching on an error.

Steps:
- [ ] Write the failing tests.
- [ ] Run them and confirm they fail.
- [ ] Implement.
- [ ] Run `(cd web && npm run typecheck && npm run test -- session)` and confirm it passes.
- [ ] Commit `feat(web): carry rule suggestions and grant/preview callbacks`.

### Task W2: AlwaysAllowDialog and card wiring

**Files:**
- Create `web/src/features/sessions/panels/AlwaysAllowDialog.tsx`. It is presentational, and all I/O comes in through props:
  ```ts
  interface Props {
    open: boolean
    block: PermBlock                       // has ruleSuggestions, tool, input
    hasRepo: boolean
    onPreview: (pattern: string, scope: Scope) => Promise<RulePreview>
    onGrant: (pattern: string, scope: Scope, confirm: boolean) => Promise<void>  // rejects with ApiRequestError
    onClose: () => void
  }
  type Scope = 'session' | 'repo' | 'global'
  ```
  - The dialog is `ui/Dialog` with `title="Always allow"`.
  - It has a `RadioGroup` of `ruleSuggestions` (pattern in mono, plus its description). The first one is selected.
  - An `Input` holds the pattern. It is pre-filled from the selected suggestion and is editable; picking a radio overwrites it.
  - A scope `RadioGroup` offers This session (default), This repo (disabled with the hint "Scratch session — no repo" when `!hasRepo`) and All sessions.
  - Under the scope picker, one muted line: "Repo and All-sessions rules apply on every host and account."
  - The preview is debounced (300 ms) through `onPreview` whenever the pattern or scope changes. It shows:
    - `description`;
    - `reasons`, with warn styling;
    - "Doesn't cover this request" when `coversRequest === false`;
    - "Also resolves N other waiting requests" when `alsoPending > 0`.
  - The Grant button is disabled while the pattern is invalid, a refusal is shown, or the pattern does not cover the request.
  - The confirm step appears when `needsConfirmation`, or after a 409 `RULE_NEEDS_CONFIRMATION`. The Grant button becomes "Confirm — allow everywhere" for global scope, or "Confirm" for a warning, and only that click sends `confirm: true`.
  - On success, close. On `PERMISSION_NOT_PENDING`, show "This request is no longer pending" with a Close button. On any other error, show its `message`.
  - Pressing Enter in the pattern field grants. The `Dialog` handles Esc.
- Modify `PermissionCard.tsx`:
  - The non-plan actions become `['allow', 'deny', 'always']`, labelled **Allow once** / **Deny** / **Always allow…**. The order and the digit mapping are unchanged.
  - "Always allow…" shows when `canAlwaysAllow && (block.ruleSuggestions?.length ?? 0) > 0`. Interactive mode **no longer hides it** (spec §3.1).
  - When neither condition holds and `canAlwaysAllow` is still true, the legacy direct `'always'` decision stays reachable only for engine-managed cards (empty suggestions). Keep it as a plain `Always allow (this session)` button that calls `onDecision(permId, 'always')`. Without it, Codex cards would lose their session accept.
  - New props: `hasRepo`, `onPreviewRule`, `onGrantRule`. With all three present, "Always allow…" opens the dialog. Without them (e.g. sub-agent pages that do not pass them), fall back to the legacy direct `'always'`.
  - The `A` key on the card container opens the chooser. Wire it through `useCardKeyboard`, or an `onKeyDown` on the container that ignores INPUT and TEXTAREA targets and calls `preventDefault`.
- Modify `PendingPermissionPanel.tsx` and its three mount sites (`SessionBody.tsx:215`, `SubAgentPage.tsx:117`, `AssistantSheet.tsx:134`) to pass the new props from `rt`. `hasRepo` comes from the session record's `repo`; find how `SessionBody` reads the session. The assistant drawer passes no rule props, because assistant sessions only ever card ViberPit tools. Check what a MUTATION ViberPit tool's suggestions look like there. If the drawer has no repo, `hasRepo` is false.
- Tests:
  - `AlwaysAllowDialog.test.tsx`:
    - pre-fill from the first suggestion;
    - editing the pattern triggers a preview;
    - repo scope disabled for scratch sessions;
    - a 409 shows the confirm step and the second click sends `confirm=true`;
    - `PERMISSION_NOT_PENDING` shows the stale message;
    - Enter grants.
  - Update `PermissionCard.test.tsx` for the new labels, the visibility rules (interactive now shows it; empty suggestions show the legacy button; a destructive card shows neither) and the `A` key. Every `/^Allow Bash/` matcher must still target the Allow-once button: its aria-label stays `Allow ${label}`, and the visible text becomes "Allow once".

Steps:
- [ ] Write the failing tests.
- [ ] Run them and confirm they fail.
- [ ] Implement.
- [ ] Run the full `(cd web && npm run typecheck && npm run lint && npm run test)`.
- [ ] Commit `feat(web): Always allow… chooser on permission cards`.

### Task W3: Visual check

- [ ] Render the card and the dialog at 375×812 and 1280×800. Use the throwaway Vite harness approach: put a scratch page under `web/` that mounts `PermissionCard` with a fixture block and mocked callbacks, and drive it with Playwright. **Delete the scratch page before committing.**
- [ ] Assert the dialog does not overflow horizontally: `document.documentElement.scrollWidth <= innerWidth`.
- [ ] Assert the buttons are at least 44 px tall on the phone viewport.
- [ ] Save the screenshots to `/tmp/always-allow-*.png`.
- [ ] If the Dialog is not usable on the phone viewport, switch the dialog to `ui/Sheet side="bottom"` below the `sm` breakpoint, following the pattern of an existing component that switches the same way (grep `Sheet` usages).

---

## PR 3: management (branch `feat/always-allow-settings`, stacked on PR 2)

### Task W4: Hooks and the Settings section

**Files:**
- `web/src/api/hooks.ts`, next to the permission-profile hooks:
  - `usePermissionRules(sessionId?: string)` (query key `['permission-rules', sessionId ?? 'all']`);
  - `useUpdatePermissionRule()` (PATCH; invalidates `['permission-rules']`);
  - `useRevokePermissionRule()` (DELETE; invalidates).
- Create `web/src/features/settings/sections/PermissionRulesSection.tsx`:
  - Group rules by scope: **All sessions**, then **Repos** (sub-grouped by `scopeKey`), then **Sessions** (`scopeKey` shown as the short session id, linking to `/sessions/{id}`).
  - Each row shows the pattern (mono), the `description`, "by {createdBy} · {relative createdAt}", and "{hitCount} hits · last {relative lastHitAt | never}".
  - Each row has **Edit** (inline Input, Save, Cancel; a 409 confirm loop identical to the dialog's, and server errors shown inline) and **Revoke** (`useConfirm`, danger tone, "Revoke this rule? Matching requests will ask again.").
  - The empty state reads: "No standing rules yet. Choose **Always allow…** on a permission card to create one."
  - A header note reads: "Repo and All-sessions rules apply on every host and account. Only a person can create one — never the assistant or automations."
  - Use the existing relative-time helper (grep `formatRelative` / `timeAgo` in `web/src/lib`).
- `settingsDefs.ts`: add `{ id: 'always-allow', group: 'sessions', label: 'Always-allow rules', icon: ShieldCheck or ListChecks (lucide), scope: 'server', component: PermissionRulesSection, description: 'Standing permission rules created from session cards, with who made each and how often it fired.', keywords: ['permission', 'allow', 'rule'] }` immediately after `profiles`. Check `pageContract.test.tsx` and any test that enumerates `DEFS`, and update them.
- Tests: `PermissionRulesSection.test.tsx`:
  - grouping;
  - the stats text;
  - edit success;
  - edit 409 → confirm → PATCH with `confirm: true`;
  - revoke goes through the confirm and calls DELETE;
  - the empty state.

  Mock `@/api/hooks` explicitly, the way the neighbouring section tests do.

Steps:
- [ ] Write the failing tests.
- [ ] Run them and confirm they fail.
- [ ] Implement.
- [ ] Run the full web gate.
- [ ] Commit `feat(web): Settings → Always-allow rules`.

### Task W5: Per-session readout

**Files:**
- Create `web/src/features/sessions/SessionRulesReadout.tsx`. It uses `usePermissionRules(sessionId)` and renders a compact list of the rules that apply (pattern, scope chip, hits) with a Revoke icon button (confirm). If the list is empty, render nothing.
- Mount it where `SessionMeta`'s `mcpSlot` content is built (find who passes `mcpSlot`, e.g. `SessionBody.tsx`), as a sibling slot `rulesSlot`. Do not add a hook call directly inside `SessionMeta` (tests render it bare).
- **Memory warning:** about 47 web test files manually mock `@/api/hooks`. Adding `usePermissionRules` to a component rendered inside `SessionBody`/`MainPaneSession` breaks those mocks.
  - Grep for `vi.mock('@/api/hooks'` in the files that render `SessionBody` or `MainPaneSession`, and add `usePermissionRules: () => ({ data: [] })` to each.
  - Alternatively, lazy-mount the readout behind the existing deferred pattern (see `DeferredMcpStatusReadout.tsx`), whichever existing tests make cheaper. Then run the FULL web test suite.
- Tests: the readout renders the rules and a revoke calls the mutation.

Steps:
- [ ] Write the failing tests.
- [ ] Run them and confirm they fail.
- [ ] Implement.
- [ ] Run the full web gate.
- [ ] Commit `feat(web): per-session standing rules readout`.
