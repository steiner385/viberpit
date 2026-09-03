# Sessions Workspace: Keyboard, List and Header (PR 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the app one keyboard-shortcut registry with one owner of "is an overlay open", add Status/Project/Environment filtering to the session list, and finish the session header — closing the keyboard debts PR 3 deliberately carried forward.

**Architecture:** `shell/shortcuts.ts` becomes the single registry: one `keydown` listener in `AppShell`, entries with a `when` predicate, and every window/document-level listener in the app re-expressed as a row. A new `useOverlayState()` replaces the two *disagreeing* DOM queries and the module-private focus-trap stack. Filter state joins `useSessionListState` — the handle the shell already shares between the list and the dock — and persists through the existing namespaced ordering helpers rather than a third mechanism.

**Tech Stack:** React 19.2 · TypeScript 6.0 · Vite 8 · Tailwind v4 (`@theme` tokens) · react-router-dom 7.18 · vitest + @testing-library/react · jest-axe.

**Spec:** `docs/superpowers/specs/2026-09-02-ui-shell-workspace-design.md` §6, which argues from `docs/superpowers/specs/2026-09-02-ui-target-state-design.md`. **Read both — and read §0 below first, because §6 is materially out of date.**

---

## §0. What this plan does NOT do, and why

A code survey against `origin/master` (post-PR-3) found sub-spec B §6 is **not implementable as written**. Three of its five parts describe data or concepts that do not exist. This plan implements what is real; the rest is deferred with a reason, not silently dropped.

### Deferred: §6.2 entirely — `session.meta`'s diff chip and CI bar

**Blocked on backend work.**

- **The diff chip (`+N −M`) has no data.** No diff-stat exists in the frontend *or* the Python server. Every `changed_files` occurrence is a path list, never a count. `server/api/sessions.py:_record_to_dict` emits no diff fields.
- **The CI bar has no data in the session payload.** `Session.prs[].checks` is a rollup *string*. The per-check types in `api/types.ts` — `CheckRow`, `CheckGroup`, `check_groups`, every `eta_*` — are **declared but dead**, zero consumers. Real per-check data lives only in the vendored pipeline's SSE feed, behind its own non-tokenised CSS.
- **There is no auto-merge boolean anywhere** to bind a toggle to — only `readyMergeGate()` and an untyped `POST /pr/ready-merge` response. The autopilot flags (`autopilotPaused`, `autopilotCooldownUntil`, `assistedBy`) are not auto-fix/auto-merge flags. Auto-fix is `prAutofixMode`, a prop threaded from `SessionList`.

A diff chip and CI toggles with nothing behind them would be decoration. §6.2 needs an API spec first.

### Deferred: §6.3 — the composer row

- **`Effort` does not exist.** No field on `SessionRuntime`, `ModelOption` or `Session`. `Cmd+Shift+E` has nothing to bind to.
- **The mode vocabulary does not map.** §6.3 asks for five labels over `PERMISSION_MODES`' **six** keys (`interactive`, `auto`, `allowlist`, `acceptEdits`, `full-auto`, `bypassPermissions`) with **no `plan` key** — "plan" is a separate binary `role="switch"` in `SessionMeta`. Which of `interactive`/`allowlist`/`full-auto` maps to which label, and whether `plan` becomes a seventh key, is a product decision.
- **Transcript view is already server-owned.** `defaultToolDetail` / `thinkingCollapsed` are app settings edited in `GeneralSection` and read-only on `SessionRuntime`. A `ccswitch.transcriptView` localStorage key would be a **second owner of "how much detail"** — the exact anti-pattern `CLAUDE.md` names as this codebase's most expensive bug shape. And "Summary" has no third `ToolDetailMode` to map to.

The composer's hand-rolled `role="menu"` divs are a real parallel-implementation problem worth fixing — but that fix belongs with the mode-vocabulary decision, not ahead of it.

### Also already done, contrary to §6.4

§6.4's delete list is stale. PR 3 **already** collapsed "the three detach handlers" into one (`useInSessionKeys`) and **already** removed `SessionView`'s `Shift+Tab` (commit `9b64fff8`, because it killed reverse-tab navigation app-wide and walked live sessions toward `bypassPermissions`). `SessionView.tsx` no longer exists.

### In scope

**§6.4** the shortcuts registry · **§6.5** list filters · **§6.1** the header's remaining pieces (click-to-rename, `StatusPill`). Views/panes are deferred with §6.3 — they need `session.panes`, and Tasks and PR panes do not exist.

---

## Global Constraints

- **One registry.** After this PR, `grep -rn "addEventListener('keydown'" web/src` outside `ui/` primitives returns exactly one hit: the registry's listener in `AppShell`.
- **Component-scoped `onKeyDown` handlers STAY.** The registry absorbs **window/document-level** listeners only. The 16 React `onKeyDown` handlers (`ui/Menu`'s roving focus and typeahead, `ui/Tabs`' arrows, `SidebarResizer`'s nudge, `useCardKeyboard`'s option picker, `Composer`'s ghost-accept and history recall, `DispatchBar`'s list navigation, `BrowserTab`'s VNC relay, …) are component behaviour, not app shortcuts. **Deleting them would be catastrophic** and §6.4's wording ("every in-component listener") must not be read that way.
- **Before adding a constant, helper or component, grep for the existing source of truth and route through it.** (`CLAUDE.md`)
- **Replaced code is deleted in the same change.** No half-dead modules.
- **`ccswitch/no-raw-palette`** governs `web/src/ui/**` and `web/src/shell/**` — and gains `web/src/features/**` in Task 1 (see DEC-2).
- **URL is state**; overlay params use `{ replace: true }`.
- Per-browser preferences live under the `ccswitch.` localStorage namespace.
- Commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- **Implementers never push and never open PRs.**

### Gates (all must pass before any task reports DONE)

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
```

This PR touches no Python; the controller runs `ruff` and `mypy` once at acceptance to prove it.

**Environment:** `npm --prefix web <cmd>` from the worktree root — **never `cd web/`** (a repo hook breaks on it). Single file: `npm --prefix web exec -- vitest run --root web <path>`; `--root web` is required or DOM tests fail with "document is not defined". `process.cwd()` is **not** stable across invocations — never write a cwd-relative path in a test. The sandbox rejects some compound commands (`&&`-chained git, heredocs, `$(...)`) — split them.

---

## Decisions

The survey raised thirteen questions the code cannot settle. These are decided here.

| # | Question | Decision | Why | Cost if wrong |
|---|---|---|---|---|
| **DEC-1** | Are `session.*` real `data-region` regions? `REGION_IDS` is a closed 7-tuple and **three** test files assert the region set by **equality**, so any new value reddens all three. | **One attribute, scoped assertions.** `session.*` stays `data-region`, and the shell's three equality assertions are scoped to `app.*`. | The umbrella already separates them — shell regions vs "session-workspace regions inside a Sessions page". A second attribute name would be a parallel vocabulary for one concept; scoping the assertions to what the shell actually owns is both smaller and more honest. | Three test predicates to widen again. |
| **DEC-2** | Where does `SessionHeader` live? `shell/regions/` is lint-governed but wrong-layer; `features/sessions/` is right-layer but ungoverned, so §6.1's "themed on tokens in every mode" would be unenforced. | **`features/sessions/`, and extend `no-raw-palette` to `web/src/features/**`.** | Moving a file to inherit a lint rule is the tail wagging the dog. Extend the rule to the layer that needs it. Do this in Task 1 so every later task is governed. | A wider lint scope may surface pre-existing violations — fix or exempt them explicitly in Task 1, do not weaken the rule. |
| **DEC-3** | `Esc` = stop the turn (Desktop parity, §6.4) vs today's `Esc` = detach to list. | **Keep `Esc` = detach in this PR.** The registry makes precedence explicit (overlay → open card → detach) so the change is later a one-row edit. | Repointing it now collides with **three** other `document` Escape listeners (`SessionCard`, `SessionActions` overflow menus, `useFocusTrap`'s modal stack) and breaks `detachKeyGuard`'s `'ArrowLeft' \| 'Escape' \| 'CtrlZ'` contract. §6.4 also never mentions `Ctrl+Z` force-detach at all. Shipping a half-repointed Esc is worse than shipping today's. | Desktop parity on `Esc` waits one PR. Flagged in the PR body. |
| **DEC-4** | What backs `when:` predicates like "menu open"? | **One `useOverlayState()`**, backed by exporting `useFocusTrap`'s existing `trapStack` depth and having `ui/Menu` register itself. The two disagreeing DOM queries are deleted and routed through it. | `useGlobalHelpKey` checks `[role="dialog"][aria-modal="true"]` while `useGlobalListNav` checks `[role="dialog"]` — they already disagree, which is a live parallel implementation. `trapStack` is authoritative but module-private and covers only `Dialog`/`Sheet`. | One module owns overlay state; if its coverage is wrong, it is wrong in one place. |
| **DEC-5** | `Cmd+*` bindings — nothing in the app binds Meta today, and `plain()` explicitly bails when Meta is held. | **Add Meta support to the matcher, and ship the `Cmd+/`, `Cmd+N`, `Cmd+W`, `Ctrl+Tab` rows.** Add the mac/other display split to `Shortcut` (`keys` is one string today). | These are the Desktop-parity bindings the spec exists to deliver, and the help sheet must show the right glyph per platform. | A binding conflicts with a browser default — test each in a real browser before claiming it works, and drop any that does. |
| **DEC-6** | `Effort` menu / `Cmd+Shift+E`. | **Dropped.** No field exists to bind to. | Inventing a UI for a concept the backend has no notion of is not a UI change. | Re-added when the model layer grows an effort field. |
| **DEC-7** | §6.5 "Group by gains **Project**" — group-by already has `repo`. | **A relabel, not a new value.** `repo` → "Project" in the option label; the value stays `repo`. | Two group-by values meaning the same thing is the parallel-implementation shape in miniature, and `LIVE_SPEC.groupByValues` is persisted — adding a value would strand saved prefs. | A label edit. |
| **DEC-8** | Where does filter state live, and how is it persisted? §6.5 says `ccswitch.listFilters`. | **In `useSessionListState`**, persisted by **extending the existing namespaced ordering pref** (`lib/sessionOrder.ts`, key `ccswitch.sessionOrder`), not a third key. | The shell shares that one handle between `ContextList` and `PromptDock`, so filter state must live there or the list and the dock disagree about which rows exist. And `ArchivedManager` reuses the same helpers — a separate key would fragment one list's state across three mechanisms. | A migration if the pref shape needs splitting later; `loadOrderPref` already validates and falls back. |
| **DEC-9** | Does the registry absorb `lib/shortcuts.ts`'s dispatch half (`matchListShortcut` / `matchHeaderShortcut`, driving `SessionCard` and `GroupHeader`)? | **Yes — one registry.** The move to `shell/shortcuts.ts` carries the list-mode dispatch with it. | `lib/shortcuts.ts` is already half a registry (22 rows, display + dispatch, three consumers). Leaving half behind produces exactly two owners of "what does this key do". | A larger Task 2 diff; the consumers' call shape is unchanged. |
| **DEC-10** | The two overflow-menu `document` Escape listeners (`SessionCard:179`, `SessionActions:54`). | **Absorb into the registry** as an `overlayOpen` precedence rule, deleting both. | They are window-level competitors for `Esc` that the spec's delete list omits; leaving them means the registry is not the owner. | If a menu stops closing on Esc, the precedence rule is wrong — pinned by a test in Task 4. |
| **DEC-11** | `ui/useFocusTrap.ts:119`'s Escape handler. | **Stays.** It is a primitive's own behaviour, not an app shortcut, and it already uses `stopImmediatePropagation` on top of a stack. | The registry reads its stack (DEC-4); it does not replace it. A focus trap that cannot close itself is not a focus trap. | None. |
| **DEC-12** | `ui/Menu` migration for the hand-rolled `role="menu"` divs. | **Out of scope** — it belongs with §6.3's composer work. | The mode vocabulary must be settled first; migrating menus whose contents then change is rework. | Hand-rolled menus live one more PR. Tracked. |
| **DEC-13** | `SessionBody`'s hand-rolled `role="tablist"` (the Browser tab). | **Untouched.** It is superseded by `session.panes`, which is deferred with §6.1's Views menu. | Replacing it without the panes it feeds would be churn. | Tracked with the panes work. |

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `web/src/shell/shortcuts.ts` | The one registry: `Shortcut`, `SHORTCUTS`, matchers, platform display. Replaces `lib/shortcuts.ts`. |
| `web/src/shell/useShortcuts.ts` | Installs the single `keydown` listener; evaluates `when`; dispatches. |
| `web/src/shell/useOverlayState.ts` | The one answer to "is an overlay open" — DEC-4. |
| `web/src/features/sessions/SessionHeader.tsx` | `session.header` — Back, chevrons, click-to-rename title, `StatusPill`, actions. |
| `web/src/session/listFilters.ts` | Filter predicate + the persisted pref shape — DEC-8. |

**Deleted:** `web/src/lib/shortcuts.ts` (moved) · `web/src/components/useGlobalHelpKey.ts` · `web/src/components/useGlobalListNav.ts` · `web/src/components/session/useInSessionKeys.ts` — all four become registry rows.

**Modified:** `web/src/shell/AppShell.tsx` · `web/src/ui/useFocusTrap.ts` (export the stack depth) · `web/src/ui/Menu.tsx` (register openness) · `web/src/components/SessionCard.tsx` · `web/src/components/GroupHeader.tsx` · `web/src/components/session/SessionActions.tsx` · `web/src/components/SessionListControls.tsx` · `web/src/features/sessions/useSessionListState.ts` · `web/src/features/sessions/MainPaneSession.tsx` · `web/src/session/useSessionOrdering.ts` · `web/src/lib/sessionOrder.ts` · `web/src/shell/regions/HelpSheet.tsx` · `web/eslint-rules/no-raw-palette.js` (scope) · `web/src/lib/detachKeyGuard.ts` (stale docstring).

---

## Task order

Tasks 1–3 build the registry's foundations (lint scope, the registry module, the overlay-state owner) without changing behaviour. Task 4 is the cutover: `AppShell` installs the listener and the four old listeners are deleted — the one task where keyboard behaviour can regress, so it carries the heaviest test burden. Tasks 5–6 add the Desktop-parity bindings. Tasks 7–9 do list filters. Task 10 finishes the header. Task 11 verifies.

**Task 4 is the risky one.** A keyboard listener that silently stops firing looks exactly like one that was never there, and the suite cannot tell the difference unless the tests assert the *behaviour* rather than the wiring.

### Plan fidelity

Tasks 1–4 and 7–8 give complete code, verified against the survey. Tasks 5–6 and 9–11 give a complete *contract* — required behaviour and the assertions the tests must make — but not full source, because they compose components whose real prop signatures are not reproduced here. **Read the actual component before writing any call site.** Where this plan contradicts the code, **the code wins and you report the contradiction** — that happened eleven times in PR 3, and every time the code was right.

---

### Task 1: extend the palette lint to `features/**`

**Files:** Modify `web/eslint-rules/no-raw-palette.js` (or wherever its scope is configured — check `web/eslint.config.js` first; the glob may live there rather than in the rule).

**Why first:** DEC-2 puts `SessionHeader` in `web/src/features/sessions/`, and §6.1 requires it "themed on tokens in every mode" — which is unenforced unless the rule reaches that layer. Doing this first means every later task in this plan is governed.

- [ ] **Step 1: Find where the scope is defined**

```
grep -rn "no-raw-palette" web/eslint.config.js web/eslint-rules/
```

The rule bans raw palette classes, `dark:`, hex in class strings, z-index literals and arbitrary px. Its current scope is `web/src/ui/**` and `web/src/shell/**`.

- [ ] **Step 2: Run it against the new scope BEFORE changing anything, to size the problem**

```
npm --prefix web exec -- eslint 'src/features/**/*.tsx' --rule '{"ccswitch/no-raw-palette":"error"}' --no-eslintrc --parser-options=ecmaVersion:2022,sourceType:module,ecmaFeatures:{jsx:true}
```

That invocation may need adjusting to your eslint version — the point is to get the violation count and file list before you widen the real config. **Report that list.**

- [ ] **Step 3: Widen the scope, then fix or exempt every violation explicitly**

Add `web/src/features/**` to the rule's glob. For each pre-existing violation: fix it if it is a straightforward token swap; if it is not (a chart colour, a vendored style), add a **narrow, commented** exemption naming the file and the reason. **Do not weaken the rule itself** to make the count zero — that would make the guard meaningless, which is the defect class this project has hit nine times.

- [ ] **Step 4: Gates and commit**

```
git add web/eslint.config.js web/eslint-rules web/src/features
git commit -m "chore(lint): no-raw-palette now governs features/ too"
```

---

### Task 2: move the registry to `shell/shortcuts.ts` and give it `when` + `run`

**Files:** Create `web/src/shell/shortcuts.ts`; delete `web/src/lib/shortcuts.ts`; modify `web/src/shell/regions/HelpSheet.tsx`, `web/src/components/SessionCard.tsx`, `web/src/components/GroupHeader.tsx`.

**Context — this is a move plus an extension, not a new file.** `lib/shortcuts.ts` is **already half a registry**: 22 rows across four groups, with `matchListShortcut` / `matchHeaderShortcut` driving `SessionCard.tsx:217` and `GroupHeader.tsx:37`, and `shortcutsByGroup` driving `HelpSheet`. Per DEC-9 the dispatch half moves too.

Today's shape:

```ts
export type ShortcutGroup = 'compose' | 'list-mode' | 'list-mode-header' | 'in-session'
export interface Shortcut {
  id: string
  keys: string            // display string for the help panel
  description: string
  group: ShortcutGroup
  action?: ListRowAction  // present ⇒ wired into the SessionCard list-mode handler
  match?: (e: KeyboardLike) => boolean
}
```

Rows: `list-mode` 8 (wired) · `list-mode-header` 6 (wired) · `compose` 8 (display-only, handled inside `DispatchBar`) · `in-session` 2 (display-only).

**What to add:**

```ts
/** Where a shortcut is allowed to fire. Evaluated against live context on each
 *  keydown; the FIRST matching row in SHORTCUTS order wins. */
export interface ShortcutContext {
  /** A modal Dialog/Sheet or an open Menu — see shell/useOverlayState. */
  overlayOpen: boolean
  /** A permission or question card is waiting for an answer. */
  hasOpenAction: boolean
  /** A session is open (`/sessions/:id`). */
  sessionOpen: boolean
  /** The Sessions section is current. */
  inSessions: boolean
  /** Kiosk is read-only: nothing that mutates may fire. */
  interactive: boolean
  /** What currently has focus, for text-entry yielding. */
  active: Element | null
}

export interface Shortcut {
  id: string
  /** Display string. Use `keysMac` when the platform binding differs. */
  keys: string
  keysMac?: string
  description: string
  group: ShortcutGroup
  action?: ListRowAction
  match?: (e: KeyboardLike) => boolean
  /** Omitted ⇒ always allowed. */
  when?: (c: ShortcutContext) => boolean
}
```

**`plain()` currently bails when Meta is held** — that is why nothing in the app binds `Cmd` today. Per DEC-5, add a `meta()` matcher alongside it rather than loosening `plain()`; the list-mode rows must keep bailing on Meta so `Cmd+A` still selects text.

Add a platform helper for the help sheet:

```ts
/** True on Apple platforms, where the help sheet shows `keysMac`. */
export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform ?? '')
}
export function displayKeys(s: Shortcut): string {
  return isApplePlatform() && s.keysMac ? s.keysMac : s.keys
}
```

- [ ] **Step 1: Move the file with `git mv` so history follows it**

```
git mv web/src/lib/shortcuts.ts web/src/shell/shortcuts.ts
```

Then update the three importers. Run the suite — **this step alone must be green**, it is a pure move.

- [ ] **Step 2: Write the failing tests for the new fields**

`web/src/shell/shortcuts.test.ts` must assert, at minimum:
- every row has a unique `id`;
- every row with an `action` also has a `match` (a wired row that cannot match is dead);
- **every row is reachable**: for each row with a `match`, a synthetic `KeyboardLike` exists that it matches — build the events from the row's own `keys` string where possible, and where not, assert explicitly that the row is display-only (`match === undefined`). *This is the guard against a row that documents a binding nobody implements — the shape that let `Shift+Tab` sit documented-but-removed.*
- `displayKeys` returns `keysMac` on Apple and `keys` elsewhere, and rows without `keysMac` return `keys` on both;
- `plain()` still bails on Meta (so list-mode rows do not eat `Cmd+A`), while `meta()` requires it.

- [ ] **Step 3: Implement, run green, gates, commit**

```
git add web/src/shell web/src/components/SessionCard.tsx web/src/components/GroupHeader.tsx
git commit -m "refactor(shell): the shortcut registry moves to shell/, gains when and platform display"
```

---

### Task 3: `useOverlayState` — one answer to "is an overlay open"

**Files:** Create `web/src/shell/useOverlayState.ts` + test; modify `web/src/ui/useFocusTrap.ts` (export the depth), `web/src/ui/Menu.tsx` (register openness).

**Context — this replaces a live parallel implementation.** Three things answer this question today and two of them disagree:

- `useGlobalHelpKey.ts:38` — `document.querySelector('[role="dialog"][aria-modal="true"]')` (modal only, so a pushed non-modal `Sheet` at ≥1280 does **not** block).
- `useGlobalListNav.ts:24` — `document.querySelector('[role="dialog"]')` (**any** dialog).
- `ui/useFocusTrap.ts:22` — `const trapStack: symbol[] = []`, authoritative for `Dialog`/`Sheet` but **module-private** and blind to `Menu`.

`ui/Menu` is not a focus trap, so no DOM query or trap stack sees it — yet a `Menu` open over the list must stop `↑`/`↓` reaching the list.

**Design:** a tiny module-level counter with a subscribe hook, incremented by `useFocusTrap` and by `Menu`. Not a React context: `useFocusTrap` is used by primitives that must not require a provider, and a context would make every existing `Dialog` test need a wrapper.

```ts
// web/src/shell/useOverlayState.ts
//
// The one answer to "is an overlay open".
//
// Before this there were three, two of which disagreed: useGlobalHelpKey asked
// for [role="dialog"][aria-modal="true"], useGlobalListNav for any
// [role="dialog"], and useFocusTrap kept an authoritative but module-private
// stack that no one else could read — and none of them saw ui/Menu at all,
// which is not a focus trap. A Menu open over the session list still has to
// stop ArrowUp reaching the list.
import { useSyncExternalStore } from 'react'

let depth = 0
const listeners = new Set<() => void>()

function emit() { for (const l of listeners) l() }

/** Called by ui/useFocusTrap and ui/Menu on open/close. Balanced pairs. */
export function pushOverlay(): void { depth += 1; emit() }
export function popOverlay(): void { depth = Math.max(0, depth - 1); emit() }

/** Non-reactive read, for the shortcut listener which runs outside render. */
export function overlayIsOpen(): boolean { return depth > 0 }

export function useOverlayState(): boolean {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => listeners.delete(l) },
    () => depth > 0,
    () => false,   // server snapshot
  )
}

/** Test-only: reset between tests, since the counter is module state. */
export function resetOverlayState(): void { depth = 0; emit() }
```

**Wiring:**
- `ui/useFocusTrap.ts` already maintains `trapStack`; call `pushOverlay()`/`popOverlay()` alongside its existing push/splice so the two cannot drift. Do **not** replace `trapStack` — DEC-11 keeps the trap's own Escape behaviour.
- `ui/Menu.tsx` — call `pushOverlay()` when `open` becomes true and `popOverlay()` on close/unmount. Balance them carefully: an unbalanced pair leaves the app permanently "overlay open" and silently kills every shortcut.
- Delete the DOM queries from `useGlobalHelpKey` and `useGlobalListNav` — but note both hooks are themselves deleted in Task 4, so if you reach Task 4 first, just delete them there.

- [ ] **Step 1: Write the failing test**

Cover: starts closed; `push` → open; balanced `push`/`pop` → closed; **unbalanced `pop` does not go negative** (two pops after one push still leaves it closed, and a subsequent push still opens it — an underflow here would make every later overlay invisible); nesting (two pushes need two pops); `useOverlayState` re-renders a subscriber on change; `resetOverlayState` clears.

Then integration: render a `Dialog` and assert `overlayIsOpen()` is true while open and false after close; same for `Menu`. **Prove the Menu wiring by removing it and watching the Menu case fail** — quote the message.

- [ ] **Step 2–4: Implement, run green, gates, commit**

```
git add web/src/shell/useOverlayState.ts web/src/shell/useOverlayState.test.ts web/src/ui/useFocusTrap.ts web/src/ui/Menu.tsx
git commit -m "feat(shell): one owner for overlay-open state, replacing two disagreeing DOM queries"
```

---

### Task 4: the cutover — one listener, four deleted

**Files:** Create `web/src/shell/useShortcuts.ts` + test; modify `web/src/shell/AppShell.tsx`, `web/src/features/sessions/MainPaneSession.tsx`, `web/src/components/SessionCard.tsx`, `web/src/components/session/SessionActions.tsx`, `web/src/lib/detachKeyGuard.ts` (stale docstring); delete `web/src/components/useGlobalHelpKey.ts`, `web/src/components/useGlobalListNav.ts`, `web/src/components/session/useInSessionKeys.ts`.

**This is the risky task.** Keyboard behaviour is invisible to the suite unless tests assert behaviour rather than wiring — a listener that silently stops firing looks exactly like one that was never there.

**The five window/document listeners being absorbed** (the survey's complete list — §6.4's is stale and short by three):

| # | File:line | Binds | Mounted by |
|---|---|---|---|
| 1 | `session/useInSessionKeys.ts:84` (window) | `Ctrl+Z` force-detach; `Escape` detach (yields on `hasOpenAction`); `ArrowLeft` detach (same) — all three via `detachKeyYieldsToTextEntry` | `MainPaneSession` |
| 2 | `components/useGlobalHelpKey.ts:42` (document) | `?` / `Shift+/` → help | `AppShell` |
| 3 | `components/useGlobalListNav.ts:28` (document) | `↑`/`↓` → list navigate | `AppShell` |
| 4 | `components/SessionCard.tsx:179` (document) | `Escape` closes the card's `⋯` menu, `stopPropagation` | each card with its menu open |
| 5 | `session/SessionActions.tsx:54` (document) | same shape | `SessionActions` |

`ui/useFocusTrap.ts:119` is **not** in this list — DEC-11 keeps it.

**Precedence, which is the whole design:** on each keydown the listener builds a `ShortcutContext` and walks `SHORTCUTS` in order, running the first row whose `match` passes **and** whose `when` allows. So:
1. `overlayOpen` rows win (a `Menu`/`Dialog` Escape closes it and nothing else fires);
2. then `hasOpenAction` (an unanswered card holds `Esc`/`←`, but **not** `Ctrl+Z` — that is the deliberate escape hatch);
3. then text-entry yielding via the existing `detachKeyYieldsToTextEntry`;
4. then the session-level gestures.

- [ ] **Step 1: Write the behaviour tests FIRST, against the current app**

Before deleting anything, write `web/src/shell/useShortcuts.test.tsx` asserting each absorbed behaviour **as behaviour**, mounted through `AppShell`:
- `?` opens the help drawer; and does **not** when focus is in a text input, when a modal is open, or in kiosk;
- `↑`/`↓` move the list highlight; and do not when a dialog is open or focus is in a form control;
- `Ctrl+Z` detaches **even with an open permission card** (the escape hatch);
- `Esc` detaches; but **not** with an open card, **not** with text in the composer, **not** when a `Menu` is open (it closes the menu instead);
- `←` detaches only from the composer's left edge (`selectionStart === selectionEnd === 0`), per `detachKeyGuard`'s composer special case;
- Escape closes an open `SessionCard` overflow menu and does **not** also detach.

**Run these against the pre-cutover code and confirm they pass.** That is what makes them a regression net rather than a description of whatever you build. Report any that do not — a red test here is a behaviour the current app does not actually have, and the plan is wrong about it.

- [ ] **Step 2: Implement `useShortcuts`**

```tsx
export function useShortcuts(ctx: () => ShortcutContext, handlers: Record<string, () => void>): void {
  const ctxRef = useRef(ctx); ctxRef.current = ctx
  const hRef = useRef(handlers); hRef.current = handlers
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented) return
      const c = ctxRef.current()
      for (const s of SHORTCUTS) {
        if (!s.match?.(e)) continue
        if (s.when && !s.when(c)) continue
        const run = hRef.current[s.id]
        if (!run) continue          // display-only row
        e.preventDefault()
        run()
        return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
```

Note the empty dep array with refs: re-registering on every context change would be a listener churn, and `MainPaneSession` re-renders constantly.

- [ ] **Step 3: Mount it in `AppShell` and delete the four hooks**

`AppShell` already computes most of the context (it has `mode`, `region.inSessions`, the workspace handle). `hasOpenAction` currently reaches `useInSessionKeys` from `MainPaneSession` — thread it up, or read it from the runtime the shell already has; **check which before writing**.

Then delete `useGlobalHelpKey.ts`, `useGlobalListNav.ts`, `useInSessionKeys.ts`, and the two overflow-menu `document` listeners. Update `detachKeyGuard.ts`'s docstring, which still names "DesktopShell and SessionView" — both deleted in PR 3.

- [ ] **Step 4: The tests from Step 1 must still pass, unchanged.**

That is the point of writing them first. If you find yourself editing them to match the new implementation, stop and report — you are describing the code rather than pinning the behaviour.

- [ ] **Step 5: Prove the listener is the only one**

```
grep -rn "addEventListener('keydown'" web/src --include=*.ts --include=*.tsx | grep -v test
```

Expected: `shell/useShortcuts.ts` and `ui/useFocusTrap.ts` only.

- [ ] **Step 6: Gates and commit**

```
git add -A web/src
git commit -m "feat(shell)!: one keydown listener; the registry owns every global shortcut"
```

---

### Task 5: the Desktop-parity bindings

**Files:** Modify `web/src/shell/shortcuts.ts`, `web/src/shell/AppShell.tsx` (+ handlers), tests.

Per DEC-5 these are new — nothing in the app binds Meta today. Ship these rows:

| Row id | Keys (mac / other) | Action | `when` |
|---|---|---|---|
| `app.help` | `⌘/` / `Ctrl+/` | open the help drawer | always (in addition to the existing `?`) |
| `app.newSession` | `⌘N` / `Ctrl+N` | → `/sessions?dialog=new` | `interactive` |
| `session.close` | `⌘W` / `Ctrl+W` | detach the open session → list | `sessionOpen && interactive` |
| `session.next` | `Ctrl+Tab` | next session | `sessionOpen` |
| `session.prev` | `Ctrl+Shift+Tab` | previous session | `sessionOpen` |
| `app.assistant` | `⌘;` / `Ctrl+;` | `?drawer=assistant` | `interactive` |

**Verify each in a real browser before claiming it works.** `Ctrl+N`, `Ctrl+W` and `Ctrl+Tab` are browser-level bindings that a page often cannot intercept — `Cmd+W` in particular closes the tab on macOS and **cannot** be prevented. If a binding cannot be captured, **drop the row and say so in your report** rather than shipping a shortcut that silently does nothing. That is the whole point of DEC-5's cost line.

Next/previous session ordering must come from the same source the list renders — `ordering.orderedSessions` on the shared `useSessionListState` handle — not a re-derived order. Two orderings would disagree the moment a filter is applied (Task 8).

- [ ] Write tests per row: fires when `when` allows, does not when it does not. For any row you drop, assert nothing binds those keys, so the drop is deliberate and visible.
- [ ] Gates; commit `feat(shell): Desktop-parity keyboard bindings`.

---

### Task 6: the help sheet shows every row, with the right glyphs

**Files:** Modify `web/src/shell/regions/HelpSheet.tsx` + test.

`HelpSheet` already generates its rows from the registry (`shortcutsByGroup`). Two changes: render `displayKeys(s)` so Apple platforms see `⌘`, and make the test **table-driven from `SHORTCUTS`** so a row added without help text fails.

That last part is the same structural guarantee `NavDef.help` gave the nav in PR 3: it made a section unable to ship without help text. Do the same here — every registry row appears in the sheet, asserted by iterating `SHORTCUTS`, not by a hand-written list that can drift.

- [ ] Test: every `SHORTCUTS` row's description appears; Apple vs non-Apple glyph rendering (stub `navigator.platform`); no row shows `undefined`.
- [ ] Gates; commit `feat(shell): the help sheet renders every registry row with platform glyphs`.

---

### Task 7: `listFilters` — the predicate and the persisted shape

**Files:** Create `web/src/session/listFilters.ts` + test; modify `web/src/lib/sessionOrder.ts`.

Per DEC-8 filters persist by **extending the existing namespaced ordering pref** (`ccswitch.sessionOrder`), not a new key. `loadOrderPref` already validates against allowed value arrays and falls back — extend that validation rather than trusting stored input.

```ts
export interface ListFilters {
  /** Session tier (see session/triageTier). Empty = no filter. */
  status: string | null
  /** GitHub nameWithOwner, matching Session.repo. */
  project: string | null
  /** Fleet host name, matching Session.host. */
  environment: string | null
}

export const NO_FILTERS: ListFilters = { status: null, project: null, environment: null }

export function filterSessions(sessions: readonly Session[], f: ListFilters): Session[]
export function isFiltered(f: ListFilters): boolean
```

**Field facts from the survey — use these, do not guess:** `Session.repo?: string | null` is a GitHub `owner/name`; `Session.host?: string | null` is a fleet host **name** (`FleetHost.name` is the join key, and **no component performs that join today**); status tier comes from `session/triageTier`. `Session` has **no** `cwd`, `branch` or `worktree`.

- [ ] Tests: each filter alone; combined (AND); `null` means "all"; an unknown stored value falls back rather than filtering everything away — **that last one matters**, because a stored host that has since been removed from the fleet would otherwise empty the list with no way to recover from the UI.
- [ ] Gates; commit `feat(sessions): list-filter predicate and persisted shape`.

---

### Task 8: wire filters into `useSessionListState`

**Files:** Modify `web/src/features/sessions/useSessionListState.ts`, `web/src/session/useSessionOrdering.ts` + tests.

Per DEC-8 filter state lives on the shared handle, because `AppShell.useSessionsWorkspace()` passes that one handle to **both** `ContextList` (→ `SessionListColumn`) and `PromptDock` (→ `DispatchBar`). Put it anywhere else and the list and the prompt bar disagree about which rows exist — and the prompt bar's `↑`/`↓` would navigate to rows the list is not showing.

**Order of operations matters:** filter **before** `useSessionOrdering`, exactly as the DEC-3 pre-spawn partition already does. `useSessionListState`'s own docstring explains why: when groups are provided, `SessionList` renders the groups and ignores its `sessions` prop, so filtering after the fact leaves the rows in the grouped list.

Add to `SessionListState`: `filters: ListFilters` and a setter. The available option lists (which projects, which hosts) derive from the **unfiltered** `sessions` — otherwise selecting a project empties the project dropdown.

- [ ] Tests: filtering changes `ordering.orderedSessions`; `nav` cursor stays valid when the filtered set shrinks under it (**a stale index here silently navigates to the wrong session**); the option lists come from the unfiltered set; pre-spawn partition still applies.
- [ ] Gates; commit `feat(sessions): filter state on the shared list handle`.

---

### Task 9: `SessionListControls` gains the three filters

**Files:** Modify `web/src/components/SessionListControls.tsx`, `web/src/features/sessions/SessionListColumn.tsx` + tests.

Today it renders exactly three controls: a search input, a Group-by `<select>` (`none` / `repo` "Repo" / `status`), and a Sort-by `<select>`. Add Status, Project and Environment `<select>`s.

Per DEC-7, **relabel `repo` to "Project"** — do not add a value. `LIVE_SPEC.groupByValues` is persisted, so a new value would strand saved prefs.

Contract: each filter is a labelled `<select>` whose first option is "All …"; selecting one narrows the list; a filter with one option or none is still rendered (so the control set does not jump around) but may be disabled; the controls stay gated on `sessions.length > 0` as today.

- [ ] Tests: each select filters; "All" clears; the Group-by option reads "Project" and its **value is still `repo`** (pin this — the relabel is exactly the kind of change a later reader "fixes" into a new value); axe clean, with the roles-and-names-only comment.
- [ ] Gates; commit `feat(sessions): Status, Project and Environment filters`.

---

### Task 10: `SessionHeader` — click-to-rename and the status pill

**Files:** Create `web/src/features/sessions/SessionHeader.tsx` + test; modify `web/src/features/sessions/MainPaneSession.tsx`.

**What already exists** (do not rebuild): `MainPaneSession`'s header renders Back, the prev/next chevrons and `SessionActions` — PR 3 ported those in from the deleted `SessionView`. Rename exists **only** inside `SessionActions`' `⋯` menu. `StatusPill` exists in `ui/` but the header does not use it; status lives in `SessionMeta`'s `statusSlot`.

**What this task adds:** extract the header into `SessionHeader.tsx` (carrying `data-region="session.header"` per DEC-1), make the title click-to-rename, and put a `StatusPill` in it.

Click-to-rename must reuse the existing rename path — `RenameDialog` and whatever mutation `SessionActions` already calls. **Grep for it and route through it**; a second rename implementation is the anti-pattern.

Keyboard: the title is a `button`, not a `div` with `onClick`; `Enter`/`Space` open rename; it has an accessible name that includes the session name.

- [ ] Tests: Back and chevrons still work (**port the existing assertions from `MainPaneSession`'s test — R9 applies, map each**); clicking the title opens rename; the pill reflects tier; `data-region="session.header"` present; axe clean.
- [ ] **DEC-1 follow-through:** scope the shell's three region-set equality assertions (`AppShell.test.tsx`, `visibilityMatrix.test.tsx`, `kiosk.integration.test.tsx`) to `app.*`. Adding `session.header` reddens all three otherwise. **Confirm they still fail when a shell region is genuinely missing** — a predicate widened too far is a guard that cannot fail.
- [ ] Gates; commit `feat(sessions): SessionHeader with click-to-rename and a status pill`.

---

### Task 11: acceptance

- [ ] **One listener:**
```
grep -rn "addEventListener('keydown'" web/src --include=*.ts --include=*.tsx | grep -v test
```
Expected exactly: `shell/useShortcuts.ts`, `ui/useFocusTrap.ts`.

- [ ] **No orphaned hooks:**
```
grep -rn "useGlobalHelpKey\|useGlobalListNav\|useInSessionKeys\|lib/shortcuts" web/src
```
Expected: nothing.

- [ ] **One overlay owner:**
```
grep -rn "role=\"dialog\"\]" web/src --include=*.ts --include=*.tsx | grep -v test
```
Expected: no `querySelector` for dialog-openness outside `useOverlayState`.

- [ ] **All six gates**, including `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` to prove no Python was touched.

- [ ] **Report the user-visible behaviour changes** for the PR body. At minimum: the new `Cmd`/`Ctrl` bindings (and any dropped as un-capturable); the help sheet showing platform glyphs; three new list filters; Group-by "Repo" relabelled "Project"; click-to-rename on the session title. And state plainly that **`Esc` still detaches rather than stopping the turn** (DEC-3), with the reason.

---

## Self-review

**Spec coverage.** §6.4 → T2, T3, T4, T5, T6. §6.5 → T7, T8, T9. §6.1 (partial) → T10. §6.2 → **deferred, §0**. §6.3 → **deferred, §0**. §6.1 Views/panes → deferred with §6.3.

**Deliberately out of scope, each with a reason in §0 or Decisions:** the diff chip and CI bar (no data end-to-end); the composer row (`Effort` does not exist, the mode vocabulary does not map, transcript view is server-owned); `ui/Menu` migration of the hand-rolled menus (DEC-12); `SessionBody`'s tablist (DEC-13); `Esc` = stop (DEC-3).

**Type consistency.** `ShortcutContext` (T2) is consumed by `useShortcuts` (T4) and every `when` (T5). `overlayIsOpen()` (T3) feeds `ShortcutContext.overlayOpen` (T4). `ListFilters` (T7) is consumed by `useSessionListState` (T8) and rendered by `SessionListControls` (T9). `Session.repo` / `Session.host` are the join keys in T7 — confirmed present; `cwd`/`branch`/`worktree` are **not** on `Session`.

**Sequencing.** T1 first so later tasks are lint-governed. T3 before T4 (the context needs an overlay answer). T4's tests are written against the **pre-cutover** app, which is what makes them a net. T7 before T8 before T9. T10 carries DEC-1's assertion-scoping, because it introduces the first `session.*` region.

**Known risk.** T5's `Cmd+W` / `Ctrl+Tab` may be un-capturable in a browser. The plan requires dropping any such row rather than shipping a dead binding — a shortcut that silently does nothing is worse than one that does not exist, because the help sheet promises it.
