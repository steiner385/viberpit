# Repo-wide palette sweep, `components/` retirement and docs (finish-line row 7) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This plan is executed as SIX sequential PRs (7a–7f), each its own SDD run with its own branch, ledger and whole-branch review**; the ledger for PR 7x lives at `.superpowers/sdd/<plan-basename>-7x/`.

**Goal:** Finish page-migration PR 11: every class string under `web/src` uses design tokens (no `dark:`, no raw palette, no hex, no `z-N`, no `min-h-[44px]`/`text-[Npx]`), the `ccswitch/no-raw-palette` rule covers the whole tree, `web/src/components/` is empty and deleted, `lib/useMediaQuery.ts` is gone, `localStorage` keys live under one namespace helper, and `docs/ACCESSIBILITY.md` / `web/README.md` describe the app that exists.

**Architecture:** The sweep is driven by the lint rule, not by hand. `eslint-rules/no-raw-palette.js` becomes **auto-fixable**: a mapping table (§Mapping) rewrites the ~40 palette/dark pairs that account for most of the 2,600 occurrences (`text-gray-500 dark:text-gray-400` → `text-ink-muted`, `bg-white dark:bg-gray-800` → `bg-surface`, `z-50` → `z-dialog`, `min-h-[44px]` → `min-h-target` …) and deletes the paired `dark:` token; anything not in the table stays an error for a human to map. Each PR widens the rule's `files` glob to the directories it converts, runs `eslint --fix` there, hand-maps the residuals, and moves the files out of `components/` into their domain (`features/<domain>/`, `ui/`, `shell/`). The order (7a tools → 7b storage → 7c media queries → 7d `components/` root, `card/`, `list/`, `settings/`, `library/` → 7e `components/session/` → 7f docs, whole-tree lint, delete the bucket) keeps every PR independently green and reviewable.

**Tech Stack:** React 19 + Tailwind v4 tokens (`web/src/index.css` `@theme`), ESLint 9 flat config with the local `ccswitch` plugin (`web/eslint-rules/`, RuleTester tests), vitest, Playwright. No `server/` changes.

**Spec:** `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` §5 row 7 (issue #1471); `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` §3 PR 11, §6 (docs produced); `docs/superpowers/specs/2026-09-02-ui-target-state-design.md` §9 success criteria, §8 row 11; `docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md` §2.5 (token discipline); `docs/superpowers/specs/2026-09-02-ui-shell-workspace-design.md` §4.1 (one layout `matchMedia` reader). Inventory (2026-09-11): 294 files / 37,673 lines under `components/`; `dark:` 813 occurrences in 87 files (810 in `components/`); raw palette 1,846 in 94 files; `z-N` 26 in 16; `min-h-[44px]` 81 in 28; `text-[Npx]` 10 in 9; 13 storage-key families in 13 files (17 test files pin them); `useMediaQuery` has 5 live call sites plus a guard-test exemption.

## Global Constraints

- Success criteria (target-state §9, verbatim): `grep -c 'dark:' web/src` → 0 outside `index.css`; `grep -cE 'bg-(gray|blue|amber|red)-' web/src` → 0 outside the chart allowlist (`web/src/features/pipeline/charts/**`). Extended for this plan: the rule's own patterns (`PALETTE`, `Z_LITERAL`, `PX_LITERAL`, `HEX`, `dark`) report zero errors with `files: ['src/**/*.{ts,tsx}']`.
- **Tokens only from `web/src/index.css`** — never add a token to satisfy a mapping; if a colour has no semantic home, pick the nearest family (§Mapping) and note it in the PR body. `-strong` fills take `text-on-strong` (never `text-white`); `accent` fills take `text-accent-ink`.
- **Class strings only.** The rule (and its fixer) act inside `className` attributes and `cn`/`cva`/`clsx`/`twMerge` calls. Hex literals in JS objects (`shell/canvas.ts` theme-color meta, SVG `stroke=` in `BrandMark`), comments, and issue numbers (`#1310`) are NOT palette debt — leave them (R3).
- **Moves are `git mv`** with the test files, so history follows; imports updated repo-wide; `vi.mock('@/components/…')` paths updated (10 occurrences in 5 files: `features/delivery/DeliveryPage.test.tsx` ×3, `features/delivery/automation/AutomationDetail.test.tsx` ×3, `features/assistant/AssistantSheet.test.tsx`, `features/sessions/MainPaneSession.actions.test.tsx` ×2, `shell/regions/DrawerHost.test.tsx`).
- **Nothing is deleted that has an importer** except `lib/useMediaQuery.ts` (7c migrates its five callers first). The inventory found no dead file under `components/`; every move keeps its tests.
- **Purge in the same PR**: a file moved out of `components/` leaves no re-export shim behind.
- Storage keys: every `localStorage`/`sessionStorage` key goes through `lib/storageKey.ts` (`storageKey('theme')` → `'ccswitch.theme'`); old keys are read once and migrated (the `notifyPref.ts` / `sidebarWidth.ts` pattern), never left as a second reader.
- Layout media queries: `shell/useViewport.tsx` stays the ONLY `matchMedia` reader for width/orientation (guard in `useViewport.test.tsx`); after 7c its `EXEMPT` set is `['src/shell/useViewport.tsx']` only.
- Web gate on every task: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist`. Full vitest every time. Playwright (`npx playwright test`) once per PR before the final commit — the sweep changes class names that e2e specs may query by text, not by class, but the Reader/Delivery/Pipeline specs must still pass.
- Bash hook: `(cd web && …)` subshells only; commit messages via `git commit -F <file>`; never pair deploy/release/rollout/promote with prod/production in command text.
- Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Branches `feat/finish-line-row-7a-tokenize`, `-7b-storage-keys`, `-7c-media-queries`, `-7d-components-root`, `-7e-session-components`, `-7f-docs-and-lint`, each off `origin/master` after the previous one merged. PR bodies `Refs #1471`; 7f's says `Fixes #1471`. Each PR non-draft + `gh pr merge --squash --auto`. Never push, open the PR or rebase from a task.

## Rulings (controller, pre-execution)

- **R1 — Six PRs, not one.** The inventory is 300 files; one PR would sit `BEHIND` master for days and be unreviewable. Each PR leaves the gate green and the lint scope strictly wider. Cost if wrong: five extra CI cycles.
- **R2 — The codemod is the lint rule's fixer.** `no-raw-palette` gains `meta.fixable = 'code'` and a `MAPPING` table; `eslint --fix` converts what the table knows, errors name the rest. Reuses the rule's `inClassContext` scoping exactly (the inventory showed bare-grep false positives: a JS object key `dark`, comments, `#NNN` issue refs). Cost if wrong: a second script that duplicates the rule's scoping.
- **R3 — Non-class hex stays.** `shell/canvas.ts` (`<meta name="theme-color">` values, mirrored in `web/index.html`) and `components/BrandMark.tsx`'s SVG stroke are not class strings and not palette debt; the target-state criteria are about class strings. `BrandMark` moves to `shell/` in 7d and keeps its SVG (the logo is brand, not theme). Cost: none.
- **R4 — Token semantics for the mapping** (§Mapping): gray text tiers → `ink` / `ink-muted` / `ink-faint`; white/gray surfaces → `surface` / `surface-raised` / `canvas`; gray borders → `edge` / `edge-strong`; blue → `accent`; red → `danger`; amber/yellow → `warning`; green/emerald → `success`; sky/indigo/violet/purple → `info`; `z-10`→`z-raised`, `z-20`→`z-sticky`, `z-30`→`z-popover`, `z-40`→`z-overlay`, `z-50`→`z-dialog` (the implementer re-checks every `z-50` site: a menu/popover is `z-popover`, an overlay `z-overlay`); `min-h-[44px]`→`min-h-target`; `text-[10px]`/`[11px]`→`text-2xs`, `text-[13px]`→`text-sm`. A residual the table cannot name is mapped by hand using the same families. The 7d/7e PR bodies enumerate every `z-` rewrite: the fixer applies the table's layer mapping and removes the error that would otherwise force the per-site check, so the PR body is where a `z-50` that should have been `z-popover` gets caught. Cost if wrong: a wrong tint, caught by the contrast test or a screenshot.
- **R5 — Destinations** (7d/7e): banners (`AllLimitedBanner`, `DaemonHealthIndicator`, `DaemonRestartBanner`, `DeployFailureNotice`, `GitCredentialBanner`, `GlobalUpdateBanner`, `NewVersionBanner`, `UpdatePendingBanner`, `DeployOverlay`, `DeployPanel`, `BrandMark`, `ErrorBoundary`) → `shell/`; `AccountsManager`, `AddAccountDialog`, `DeviceCodePanel`, `ReauthBrowserPanel`, `UsageBadge` → `features/accounts/` (new); `BacklogGroupsPanel`, `ConsolidatePanel`, `ManualDispatchDialog`, `ScheduleEditor` → `features/delivery/automation/`; `BrowserTab` → `features/sessions/browser/`; `SessionCard`, `SessionList`, `SessionListControls`, `SidebarResizer`, `SubAgentRows`, `ArchivedSessionCard`, `ArchivedSessionList`, `AdoptPanel`, `PendingApprovalSection`, `QueuedSection`, `DispatchBar`, `dispatchError.ts`, `PrList`, `PrPill` → `features/sessions/list/`; `EmptyState` (components') is folded into `ui/EmptyState` (one implementation), `GroupedList`, `GroupHeader`, `ListSearchInput`, `listControlStyles.ts`, `PermanentDeleteDialog`, `card/*`, `list/*` → `ui/list/` (`PermanentDeleteDialog` → `features/sessions/list/`); `settings/*`, `library/*` → `features/settings/sections/`; `session/*` → `features/sessions/transcript/` (rendering: `Turn`, `TranscriptView`, block/result cards, `Markdown`, `DiffView`, …), `features/sessions/composer/` (`Composer`, `SessionComposer`, `MicButton`, `AttachmentChips`, `useImageAttachments`, `useSessionDraft`, `heicConvert`, `CommandPalette`, `CommandResultOverlay`), `features/sessions/panels/` (`Pending*Panel`, `YourTurnPanel`, `QuestionCard`, `PermissionCard`, `ModelSwitchDialog`), `features/sessions/` root (`SessionBody`, `SessionMeta`, `SessionActions`, `SessionRuntimeProvider`, `SessionAriaLive`, `StatusBar`, `WorkingIndicator`, `workingWords`, `brailleSpinner`, `elapsed`, `SubAgentList`, `subAgentDuration`, `SessionGoalText`, `SessionPlaceholder`, `ExportMenu`, `useCardKeyboard`). Cost if wrong: a second `git mv`.
- **R6 — `useMediaQuery` retirement** (7c): `useViewport` gains `reducedMotion: boolean` and keeps `coarsePointer`; `CardFrame`'s ≥1024 read becomes `width === 'desktop' || width === 'wide'`; `SessionCard`/`CardFrame` read `reducedMotion`; `BrowserTab` reads `coarsePointer`; `shell/theme.tsx` keeps a private `matchMedia('(prefers-color-scheme: dark)')` subscription inside the provider (not a layout query, and the theme provider is the one legitimate reader); `useViewport.tsx` inlines the subscription helper as a module-private function. `lib/useMediaQuery.ts` deleted; `EXEMPT` shrinks to the one file. Cost if wrong: one hook.
- **R7 — Storage namespace** (7b): `lib/storageKey.ts` exports `storageKey(name: string): string` → `` `ccswitch.${name}` `` and `migrateStorageKey(storage, from, to)` (copy-then-delete, idempotent). Keys renamed: `prdash.collapsed` → `ccswitch.pipeline.collapsed`, `workspace.focusedPipeline` → `ccswitch.pipeline.focused`, `ccswitch:auto-reloaded-from-sha` → `ccswitch.autoReloadedFromSha` (sessionStorage), `ccswitch:draft:<id>` → `ccswitch.draft.<id>`. Keys already `ccswitch.<x>` keep their string (the helper produces the identical value) so the 17 pinning tests only change for the four renamed keys. Cost if wrong: one more migration line.
- **R8 — Playwright viewport projects** (deferred from row 5 R7): 7f adds `projects: [phone 390×844, tablet 820×1180, desktop 1440×900]` to `web/playwright.config.ts` and removes the per-spec `test.use({ viewport })` where a spec already loops viewports itself (`delivery.spec.ts`, `pipeline.spec.ts`, `archived-reader.spec.ts` keep their loops — projects would triple them; ruling: projects apply to specs that do NOT set a viewport; specs with their own loop opt out with `test.use({ viewport: null })`… simpler: **do not add projects**; the three page specs already cover the three widths and kiosk. Target-state §9's "verified by Playwright" is met by those specs. Cost: none; documented in `web/README.md`.
- **R9 — Emoji glyphs**: only glyphs used AS ICONS convert to lucide (`✓`/`✗` in `ToolCallCard` → `Check`/`X`; `📎`/`✨` in `DispatchBar` → `Paperclip`/`Sparkles`; the pipeline `definitions.ts` label glyphs `🔔 ⚠ 💰 🛡` → `Bell`/`AlertTriangle`/`Coins`/`Shield` rendered by the consumer). Emoji inside user-facing copy or test fixtures stay. Cost if wrong: an icon swap.
- **R10 — `text-white` beside an accent/-strong fill is reported** (`whiteOnFill`, no fix): the ink rule above is enforced by the rule, not by review. `bg-accent` flips with the theme (white on the dark-mode `--t-accent` is ~2.7:1), and the `-strong` fills are held theme-invariant precisely so `--t-on-strong` stays AA on them, so the ink must be `text-accent-ink` / `text-on-strong`. Which one applies depends on the fill, so the fixer reports and the implementer hand-maps. Cost if wrong: one hand map per site.

## Mapping (the fixer table — single source of truth, lives in `web/eslint-rules/no-raw-palette.js`)

Keys are the LIGHT class (any variant prefix such as `hover:`/`focus-visible:`/`placeholder:` is preserved on the output, as is a `!` important marker — leading in v3, trailing in v4).

A token's **partner key** is its non-`dark` variant chain plus its utility prefix (the first segment of the base): `hover:bg-gray-100` and `dark:hover:bg-gray-700` both key to `hover:|bg`; `text-gray-500` keys to `|text`. A `dark:` token is deleted **iff a light token with the SAME partner key mapped** — in the same string, in a sibling quasi of the same template literal, or in a sibling *unconditional* string argument (a `Literal`/`TemplateLiteral` directly in the args list, never one behind `&&`/`?:` or inside an object) of the enclosing `cn`/`cva`/`clsx`/`twMerge` call. Sibling arguments count because `cn` is `twMerge(clsx(…))` and the last argument wins, so a `dark:`-only argument is only meaningful beside its light sibling.

A `dark:` token with **no** same-key mapped partner is left exactly as written and keeps an unfixable `dark` error — it is a hand-mapping residual. The fixer never strips `dark:` and emits the base's mapping: the dark value is not the light value, so that would apply the dark treatment in light mode and (under last-wins) clobber the light token beside it.

```js
// text (ink tiers)
'text-gray-900': 'text-ink', 'text-gray-800': 'text-ink', 'text-gray-700': 'text-ink',
'text-gray-100': 'text-ink', 'text-gray-200': 'text-ink', 'text-gray-300': 'text-ink',   // dark-side partners
'text-gray-600': 'text-ink-muted', 'text-gray-500': 'text-ink-muted', 'text-gray-400': 'text-ink-muted',
'text-slate-500': 'text-ink-muted', 'text-slate-400': 'text-ink-muted',
'text-white': null,                       // untouched unless the fill is accent/-strong (hand-mapped)
// surfaces
'bg-white': 'bg-surface', 'bg-gray-800': 'bg-surface',
'bg-gray-50': 'bg-canvas', 'bg-gray-900': 'bg-canvas',
'bg-gray-100': 'bg-surface-raised', 'bg-gray-200': 'bg-surface-raised', 'bg-gray-700': 'bg-surface-raised', 'bg-gray-600': 'bg-surface-raised',
// edges
'border-gray-200': 'border-edge', 'border-gray-700': 'border-edge', 'divide-gray-200': 'divide-edge', 'divide-gray-700': 'divide-edge',
'border-gray-300': 'border-edge-strong', 'border-gray-600': 'border-edge-strong',
// accent (blue)
'text-blue-600': 'text-accent', 'text-blue-400': 'text-accent', 'text-blue-500': 'text-accent', 'text-blue-300': 'text-accent', 'text-blue-700': 'text-accent',
'bg-blue-600': 'bg-accent', 'bg-blue-500': 'bg-accent', 'bg-blue-700': 'bg-accent/90',
'bg-blue-50': 'bg-accent-wash', 'bg-blue-900/30': 'bg-accent-wash', 'bg-blue-900/20': 'bg-accent-wash', 'bg-blue-100': 'bg-accent-wash',
'border-blue-400': 'border-accent', 'border-blue-500': 'border-accent', 'border-blue-300': 'border-accent', 'border-blue-600': 'border-accent',
'ring-blue-500': 'ring-accent', 'ring-blue-400': 'ring-accent', 'ring-blue-600': 'ring-accent',
// danger (red)
'text-red-600': 'text-danger', 'text-red-400': 'text-danger', 'text-red-700': 'text-danger', 'text-red-300': 'text-danger', 'text-red-500': 'text-danger', 'text-red-800': 'text-danger',
'bg-red-50': 'bg-danger-wash', 'bg-red-950/30': 'bg-danger-wash', 'bg-red-900/30': 'bg-danger-wash', 'bg-red-950': 'bg-danger-wash', 'bg-red-100': 'bg-danger-wash',
'bg-red-600': 'bg-danger-strong', 'bg-red-500': 'bg-danger-strong', 'bg-red-700': 'bg-danger-strong/90',
'border-red-300': 'border-danger', 'border-red-700': 'border-danger', 'border-red-500': 'border-danger', 'border-red-400': 'border-danger', 'border-red-800': 'border-danger',
'ring-red-500': 'ring-danger',
// warning (amber / yellow)
'text-amber-600': 'text-warning', 'text-amber-400': 'text-warning', 'text-amber-700': 'text-warning', 'text-amber-300': 'text-warning', 'text-amber-500': 'text-warning', 'text-amber-800': 'text-warning', 'text-amber-200': 'text-warning',
'text-yellow-600': 'text-warning', 'text-yellow-400': 'text-warning', 'text-yellow-700': 'text-warning', 'text-yellow-300': 'text-warning',
'bg-amber-50': 'bg-warning-wash', 'bg-amber-900/40': 'bg-warning-wash', 'bg-amber-900/30': 'bg-warning-wash', 'bg-amber-900/20': 'bg-warning-wash', 'bg-amber-100': 'bg-warning-wash', 'bg-yellow-50': 'bg-warning-wash', 'bg-yellow-900/30': 'bg-warning-wash',
'bg-amber-500': 'bg-warning-strong', 'bg-amber-600': 'bg-warning-strong',
'border-amber-300': 'border-warning', 'border-amber-600': 'border-warning', 'border-amber-700': 'border-warning', 'border-amber-400': 'border-warning', 'border-amber-500': 'border-warning', 'border-yellow-300': 'border-warning', 'border-yellow-700': 'border-warning',
'ring-amber-500': 'ring-warning',
// success (green / emerald)
'text-green-600': 'text-success', 'text-green-400': 'text-success', 'text-green-700': 'text-success', 'text-green-300': 'text-success', 'text-green-500': 'text-success',
'text-emerald-600': 'text-success', 'text-emerald-400': 'text-success', 'text-emerald-700': 'text-success', 'text-emerald-500': 'text-success',
'bg-green-50': 'bg-success-wash', 'bg-green-900/30': 'bg-success-wash', 'bg-green-100': 'bg-success-wash', 'bg-emerald-50': 'bg-success-wash', 'bg-emerald-900/30': 'bg-success-wash',
'bg-green-600': 'bg-success-strong', 'bg-green-500': 'bg-success-strong', 'bg-emerald-500': 'bg-success-strong', 'bg-emerald-600': 'bg-success-strong',
'border-green-300': 'border-success', 'border-green-700': 'border-success', 'border-green-500': 'border-success', 'border-emerald-500': 'border-success',
// info (sky / indigo / violet / purple)
'text-sky-600': 'text-info', 'text-sky-400': 'text-info', 'text-indigo-600': 'text-info', 'text-indigo-400': 'text-info', 'text-indigo-300': 'text-info',
'text-violet-600': 'text-info', 'text-violet-400': 'text-info', 'text-violet-300': 'text-info', 'text-purple-600': 'text-info', 'text-purple-400': 'text-info', 'text-purple-300': 'text-info',
'bg-sky-50': 'bg-info-wash', 'bg-indigo-50': 'bg-info-wash', 'bg-violet-50': 'bg-info-wash', 'bg-purple-50': 'bg-info-wash', 'bg-violet-900/30': 'bg-info-wash', 'bg-indigo-900/30': 'bg-info-wash', 'bg-purple-900/30': 'bg-info-wash',
'bg-violet-600': 'bg-info-strong', 'bg-indigo-600': 'bg-info-strong', 'bg-purple-600': 'bg-info-strong',
'border-violet-300': 'border-info', 'border-violet-700': 'border-info', 'border-indigo-300': 'border-info', 'border-indigo-700': 'border-info', 'border-purple-300': 'border-info', 'border-purple-700': 'border-info',
// layers and sizes
'z-10': 'z-raised', 'z-20': 'z-sticky', 'z-30': 'z-popover', 'z-40': 'z-overlay', 'z-50': 'z-dialog',
'min-h-[44px]': 'min-h-target', 'min-w-[44px]': 'min-w-target', 'h-[44px]': 'h-target', 'w-[44px]': 'w-target',
'text-[10px]': 'text-2xs', 'text-[11px]': 'text-2xs', 'text-[12px]': 'text-xs', 'text-[13px]': 'text-sm',
```

Hand-mapping rules for residuals (the rule reports them): a `text-white` on an `accent` fill → `text-accent-ink`; on a `-strong` fill → `text-on-strong`; `bg-gray-*` used as a *disabled* fill → `bg-surface-raised text-ink-faint`; `hover:text-gray-*` → `hover:text-ink`; `placeholder:text-gray-*` → `placeholder:text-ink-faint`; `focus:ring-*` → `focus-visible:ring-accent`; a colour that encodes *identity* (an account's avatar tint, an engine badge) → the `Badge`/`StatusPill` tone API rather than a class. `hover:bg-gray-50/100` → `hover:bg-surface-raised` is no longer hand-mapped: the fixer itself detects a hover/focus/focus-visible/active variant landing on `bg-canvas` (a no-op on a canvas-backed page) and emits `bg-surface-raised` in its place — see `rewriteClassString`'s `hasStateVariant` check in `no-raw-palette.js`.

## File map

| PR | Files | Responsibility |
|----|-------|----------------|
| 7a | `web/eslint-rules/no-raw-palette.js` (+ `.test.ts`), `web/eslint.config.js`, `web/src/session/statusTier.ts`, `web/src/projects/DependencyPanel.tsx`, `web/package.json` (`lint:fix-tokens` script) | Fixable rule + mapping; scope = everything except `src/components/**`; the two stray files converted. |
| 7b | `web/src/lib/storageKey.ts` (+ test), the 13 key sites, the 17 pinning tests | One namespace helper; four renames with migration. |
| 7c | `web/src/shell/useViewport.tsx` (+ test `EXEMPT`), `shell/theme.tsx`, `components/BrowserTab.tsx`, `components/SessionCard.tsx`, `components/card/CardFrame.tsx`; delete `web/src/lib/useMediaQuery.ts` | One layout reader; `reducedMotion` on the viewport. |
| 7d | `git mv` of `components/` root files, `card/`, `list/`, `settings/`, `library/` per R5; `ui/EmptyState` fold; ESLint scope unchanged (destinations already covered) | ~157 files moved and converted. |
| 7e | `git mv` of `components/session/**` per R5 (three tasks: transcript / composer+panels / session root) | ~137 files moved and converted. |
| 7f | `web/eslint.config.js` (`files: ['src/**/*.{ts,tsx}']`), delete `web/src/components/`, `docs/ACCESSIBILITY.md`, `web/README.md`, `docs/product/features/web-dashboard-mobile.md`, the four `docs/superpowers/specs` status cells | Whole-tree lint, docs, close #1471. |

---

## PR 7a — the fixer and its scope

### Task 1: `no-raw-palette` becomes auto-fixable

**Files:**
- Modify: `web/eslint-rules/no-raw-palette.js`
- Modify: `web/eslint-rules/no-raw-palette.test.ts`
- Modify: `web/package.json` (add `"lint:fix-tokens": "eslint --fix --fix-type problem"` — NOT `--rule 'ccswitch/no-raw-palette: error'`, which fails with "could not find plugin ccswitch" on any path outside the rule's configured scope. The config already sets the rule to `error` in scope and `no-raw-palette` is `type: 'problem'`, so `--fix-type problem` restricts the fixing to it.)

**Interfaces:**
- Produces: `export const MAPPING: Record<string, string | null>` (the §Mapping table) and `export function rewriteClassString(text: string): { out: string; changed: boolean }` — pure, unit-tested, used by the fixer.
- Fixer contract: for a `Literal`/`TemplateElement` in class context whose `rewriteClassString` output differs, report ONE fix replacing the node's raw text with the same quoting (`JSON.stringify`-free: reuse the original quote character; for a `TemplateElement` replace only `node.value.raw`). Tokens not in the table are still reported as errors (no fix). Whitespace between tokens is collapsed to single spaces in the rewritten string.

- [ ] **Step 1: Failing RuleTester cases** — open `web/eslint-rules/no-raw-palette.test.ts`, read its existing `valid`/`invalid` shape, and add:

```ts
// fixable: paired light/dark → one token, dark deleted
{
  code: `<div className="text-gray-500 dark:text-gray-400 bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700" />`,
  output: `<div className="text-ink-muted bg-surface border-edge" />`,
  errors: [{ messageId: 'palette' }, { messageId: 'dark' }, { messageId: 'palette' }, { messageId: 'dark' }, { messageId: 'palette' }, { messageId: 'dark' }],
},
// fixable: variant prefixes survive; z and px literals map
{
  code: `cn('hover:bg-gray-100 dark:hover:bg-gray-700 z-50 min-h-[44px] text-[11px]', open && 'ring-blue-500')`,
  output: `cn('hover:bg-surface-raised z-dialog min-h-target text-2xs', open && 'ring-accent')`,
  errors: 6,
},
// not fixable: unknown palette stays an error with no output change
{
  code: `<div className="bg-fuchsia-500" />`,
  output: null,
  errors: [{ messageId: 'palette' }],
},
// dark token with no same-key light partner: a residual, left exactly as written
{
  code: `<div className="dark:text-gray-300" />`,
  output: null,
  errors: [{ messageId: 'dark' }],
},
// template literal: only the static chunk is rewritten (an emptied quasi keeps
// one space so the interpolations around it cannot glue together)
{
  code: 'cn(`text-gray-500 ${x} dark:text-gray-400`)',
  output: 'cn(`text-ink-muted ${x} `)',
  errors: 2,
},
```
and a unit block for the pure helper:
```ts
import { rewriteClassString } from './no-raw-palette.js'
describe('rewriteClassString', () => {
  it('leaves already-token strings alone', () => {
    expect(rewriteClassString('bg-surface text-ink')).toEqual({ out: 'bg-surface text-ink', changed: false })
  })
  it('drops a dark twin only when a light token with the SAME partner key mapped', () => {
    expect(rewriteClassString('text-gray-500 dark:text-gray-400').out).toBe('text-ink-muted')
    expect(rewriteClassString('border-gray-100 dark:border-gray-700')).toEqual({
      out: 'border-gray-100 dark:border-gray-700', changed: false,
    })
    expect(rewriteClassString('text-gray-500 dark:border-gray-700').out).toBe('text-ink-muted dark:border-gray-700')
  })
  it('collapses duplicate tokens produced by a pair mapping to the same token', () => {
    expect(rewriteClassString('text-gray-700 dark:text-gray-300 text-gray-900').out).toBe('text-ink')
  })
})
```

- [ ] **Step 2: Run** — `(cd web && npx vitest run eslint-rules/no-raw-palette.test.ts)`. Expected: FAIL (no `output` produced, `rewriteClassString` not exported).

- [ ] **Step 3: Implement** — in `web/eslint-rules/no-raw-palette.js`:

```js
export const MAPPING = { /* §Mapping table, verbatim */ }

/** Split `hover:focus-visible:bg-gray-100` into ['hover:focus-visible:', 'bg-gray-100']. */
function splitVariants(token) {
  const i = token.lastIndexOf(':')
  return i === -1 ? ['', token] : [token.slice(0, i + 1), token.slice(i + 1)]
}

export function rewriteClassString(text) {
  const tokens = text.split(/\s+/).filter(Boolean)
  const out = []
  let changed = false
  const mappedLightBases = new Set()
  // pass 1: light tokens
  for (const raw of tokens) {
    const [variants, base] = splitVariants(raw)
    if (variants.split(':').includes('dark')) continue
    const to = MAPPING[base]
    if (to) { out.push(variants + to); mappedLightBases.add(base); changed = true }
    else out.push(raw)
  }
  // pass 2: dark tokens — drop when a light token in the same string mapped, else map by base
  for (const raw of tokens) {
    const [variants, base] = splitVariants(raw)
    const parts = variants.split(':').filter(Boolean)
    if (!parts.includes('dark')) continue
    const to = MAPPING[base]
    if (to && mappedLightBases.size > 0) { changed = true; continue }
    if (to) { out.push(parts.filter((p) => p !== 'dark').map((p) => p + ':').join('') + to); changed = true; continue }
    out.push(raw)
  }
  // dedupe while keeping order
  const seen = new Set()
  const deduped = out.filter((t) => (seen.has(t) ? false : (seen.add(t), true)))
  return { out: deduped.join(' '), changed: changed || deduped.length !== tokens.length }
}
```
Wire the fixer: in `check(...)`, compute `const { out, changed } = rewriteClassString(text)` once per node; for the FIRST report on that node attach `fix(fixer)` when `changed`: for `Literal` → `fixer.replaceText(node, node.raw[0] + out + node.raw[0])`; for `TemplateElement` → `fixer.replaceTextRange([node.range[0] + 1, node.range[1] - 1], out)` (ESLint's `TemplateElement` range includes the backtick/`${` delimiters — check with the RuleTester cases; adjust the offsets until the template case passes). Subsequent reports on the same node carry no fix (ESLint applies one fix per node per pass; `--fix` iterates up to 10 passes). Set `meta.fixable = 'code'`.

Note on pass ordering: pass 2 is **partner-keyed**, not string-wide (§Mapping). A dark token is dropped iff a light token with the same partner key (non-`dark` variant chain + utility prefix) mapped — in this string, in a sibling quasi, or in an unconditional sibling argument of the enclosing class call (seed `externalLightKeys` from those). So `text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-700` maps both light tokens and drops both dark ones, while `border-gray-100 dark:border-gray-700` (light side unmapped) changes nothing at all. A dark token with no same-key partner is emitted verbatim and its `dark` error carries no fix; `dark:` is never stripped.

- [ ] **Step 4: Run** — same command. Expected: PASS.

- [ ] **Step 5: Gate** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist`. The existing scope (`ui/shell/features/pipeline`) must report nothing new (those trees are already on tokens).

- [ ] **Step 6: Commit** — `feat(lint): no-raw-palette is auto-fixable — the mapping table is the sweep's codemod` / `Refs #1471` / trailer.

### Task 2: Widen the scope to everything but `components/`; convert the two strays

**Files:**
- Modify: `web/eslint.config.js:23-31` → `files: ['src/**/*.{ts,tsx}'], ignores: ['src/components/**', 'src/features/pipeline/charts/**']`; update the comment: "…the whole tree in row 7 (7a: everything but `components/`; 7f: everything)".
- Modify: `web/src/session/statusTier.ts:31` (`SLATE`), `web/src/projects/DependencyPanel.tsx:31`. (Done; the 7a fix wave then folded `SLATE` into `CALM` — they had become the same string, i.e. two names for one constant.)

- [ ] **Step 1: Widen, then run the fixer** — `(cd web && npm run lint:fix-tokens -- src/session src/projects src/lib src/hooks src/api src/obs src/vnc src/ws src/pwa src/initiatives src/test)`; then `npm run lint`. Expected: after the fixer, zero `ccswitch/no-raw-palette` errors (the inventory found only the two files; if the fixer leaves a residual, hand-map it per §Mapping).
- [ ] **Step 2: Check the diff** — `git diff --stat`; expected: exactly the two files (plus config). Read each hunk; `statusTier.ts` `SLATE` becomes `'text-ink-muted'`; `DependencyPanel` `bg-amber-500 text-amber-600` becomes `bg-warning-strong text-warning` — the reviewer will judge whether `text-warning` on `bg-warning-strong` is intended; if the class pair styles the same element, use `bg-warning-strong text-on-strong`.
- [ ] **Step 3: Gate + Playwright** — full gate, then `(cd web && npm run build && npx playwright test); rm -rf web/dist`.
- [ ] **Step 4: Commit** — `chore(lint): token rule covers every tree except components/` / `Refs #1471`.

PR 7a body cites target-state §8 row 11 and says which directories are now under the rule. Open, auto-merge; deploy after merge (web-only).

---

## PR 7b — one storage namespace

### Task 1: `lib/storageKey.ts`

**Files:** Create `web/src/lib/storageKey.ts`, `web/src/lib/__tests__/storageKey.test.ts`.

**Interfaces:** `export const STORAGE_NAMESPACE = 'ccswitch'`; `export function storageKey(name: string): string` → `` `${STORAGE_NAMESPACE}.${name}` ``; `export function migrateStorageKey(storage: Storage, from: string, to: string): void` — if `storage.getItem(from) !== null && storage.getItem(to) === null` copy it to `to`; then `removeItem(from)`; swallow `SecurityError`/quota exceptions (private mode).

- [ ] Failing test: `storageKey('theme') === 'ccswitch.theme'`; `migrateStorageKey` copies once, never overwrites an existing `to`, removes `from`, and is a no-op when `from` is absent; a throwing `Storage` stub does not throw out.
- [ ] Implement; run; commit `feat(lib): storageKey — one namespace for persisted UI state` / `Refs #1471`.

### Task 2: Route every key through it; rename the four outliers with migration

**Files:** the 13 sites from the inventory: `pipeline/PipelineView.tsx:32`, `pipeline/notifyPref.ts:3-4`, `features/settings/lastSection.ts:4`, `features/pipeline/useFocusedPipeline.ts:8`, `hooks/useAutoReload.ts:12`, `lib/enginePreference.ts:18`, `lib/sidebarWidth.ts:16,18`, `lib/lastRepo.ts:7`, `components/DeployPanel.tsx:16`, `shell/theme.tsx:6`, `shell/railExpanded.ts:3`, `shell/drawerWidth.ts:24`, `lib/groupCollapse.ts:6-7`, `lib/sessionOrder.ts:19-20`, `components/session/useSessionDraft.ts:7`; and the 17 pinning tests listed in the inventory (§8) — only the FOUR renamed keys change in tests: `'prdash.collapsed'` → `storageKey('pipeline.collapsed')`, `'workspace.focusedPipeline'` → `storageKey('pipeline.focused')`, `'ccswitch:auto-reloaded-from-sha'` → `storageKey('autoReloadedFromSha')`, `` `ccswitch:draft:${id}` `` → `storageKey(\`draft.${id}\`)`.

- [ ] For each renamed key add, at the module's first read, `migrateStorageKey(localStorage, OLD, NEW)` (sessionStorage for `useAutoReload`), and a test that seeds the OLD key and asserts the value is read under NEW and OLD is gone (`notifyPref.ts` already has this exact test shape — copy it).
- [ ] `grep -rn "localStorage\.\(get\|set\|remove\)Item(\s*'" web/src --include=*.ts --include=*.tsx | grep -v test | grep -v storageKey` → 0 (every literal key routed through the helper).
- [ ] Gate; commit `refactor(web): every persisted key through storageKey; four outliers migrated` / `Refs #1471`. PR, auto-merge, deploy.

---

## PR 7c — one media-query reader

### Task 1: `useViewport.reducedMotion`; retire `lib/useMediaQuery.ts`

**Files:** `web/src/shell/useViewport.tsx`, `web/src/shell/useViewport.test.tsx` (`EXEMPT`, plus a `reducedMotion` case), `web/src/shell/theme.tsx`, `web/src/components/BrowserTab.tsx:43`, `web/src/components/SessionCard.tsx:153`, `web/src/components/card/CardFrame.tsx:76-77`; delete `web/src/lib/useMediaQuery.ts` (+ its test if one exists — `ls web/src/lib/__tests__ | grep -i media`).

- [ ] `useViewport.tsx`: move the hook body of `lib/useMediaQuery.ts` in as a module-private `function useMediaQuery(query: string): boolean` (not exported); add `reducedMotion: boolean` to `Viewport` (`'(prefers-reduced-motion: reduce)'`); update the provider docstring's exemption list (only `theme.tsx` remains, for colour scheme).
- [ ] `theme.tsx`: replace `useMediaQuery('(prefers-color-scheme: dark)')` with a private `useSystemDark()` in the same file (same subscription pattern, 10 lines).
- [ ] `BrowserTab.tsx`: `useViewport().coarsePointer`. `SessionCard.tsx`: `useViewport().reducedMotion`. `CardFrame.tsx`: `const { width, reducedMotion } = useViewport()`; `const wide = width === 'desktop' || width === 'wide'` replaces the 1024px read. Their tests: wrap renders in `ViewportProvider` where they are not already (grep `stubMatchMedia` / `LAYOUT_MODE_QUERIES` in `web/src/test/` for the existing helper).
- [ ] `useViewport.test.tsx`: `EXEMPT = new Set(['src/shell/useViewport.tsx'])`; the guard walk now also asserts `!/useMediaQuery/.test(source)` outside the exempt file (the primitive is gone; a re-import is the regression).
- [ ] Gate + Playwright; commit `refactor(shell): useViewport owns reduced-motion; lib/useMediaQuery.ts retired` / `Refs #1471`. PR, auto-merge, deploy.

---

## PR 7d — `components/` root, `card/`, `list/`, `settings/`, `library/`

Each task: `git mv` the files (with their `__tests__/` siblings, renamed to `<Name>.test.tsx` beside the file — the `features/` convention), fix imports repo-wide (`grep -rln "@/components/<Name>"` → sed), run `npm run lint:fix-tokens -- <destination dirs>`, hand-map residuals per §Mapping, tidy the empty-string arguments the fixer leaves behind when a `dark:`-only sibling argument is fully dropped (`cn('bg-accent', '')` → `cn('bg-accent')`), run the covering tests, then the gate. One commit per task; the PR body lists every `z-` rewrite (R4).

### Task 1: shell chrome — the eight banners, `DeployOverlay`, `DeployPanel`, `BrandMark`, `ErrorBoundary` → `web/src/shell/`
### Task 2: accounts — `AccountsManager`, `AddAccountDialog`, `DeviceCodePanel`, `ReauthBrowserPanel`, `UsageBadge` → `web/src/features/accounts/` (new dir; `AccountsManager` is 971 lines — do not split it in this PR, note it in the report)
### Task 3: delivery — `BacklogGroupsPanel`, `ConsolidatePanel`, `ManualDispatchDialog`, `ScheduleEditor` → `web/src/features/delivery/automation/` (update the 6 `vi.mock` paths in the two delivery tests)
### Task 4: list primitives — `GroupedList`, `GroupHeader`, `ListSearchInput`, `listControlStyles.ts`, `card/*`, `list/*` → `web/src/ui/list/`; `components/EmptyState.tsx` is deleted and its importers switch to `ui/EmptyState` (compare the two props APIs first; if `components/EmptyState` has a prop `ui/EmptyState` lacks, add it to `ui/EmptyState` in this task — one implementation)
### Task 5: sessions list — `SessionCard`, `SessionList`, `SessionListControls`, `SidebarResizer`, `SubAgentRows`, `ArchivedSessionCard`, `ArchivedSessionList`, `AdoptPanel`, `PendingApprovalSection`, `QueuedSection`, `DispatchBar`, `dispatchError.ts`, `PrList`, `PrPill`, `PermanentDeleteDialog`, `BrowserTab` → `web/src/features/sessions/list/` (`BrowserTab` → `features/sessions/browser/`). Also fold the `SubAgentStatus` colour/label maps: `SubAgentRows` and (in 7e) `SubAgentList`/`SessionMeta` import `STATUS_TONE`/`STATUS_LABEL` from `features/sessions/SubAgentPage.tsx` — move those two maps to a new `web/src/features/sessions/subAgentStatus.ts` (exported) and have `SubAgentPage` import them too (#1471 carry-over 1).
### Task 6: settings — `components/settings/*`, `components/library/*` → `web/src/features/settings/sections/`; update `features/settings/settingsDefs.ts` imports.

Acceptance for 7d: `ls web/src/components` → only `session/` (and its `__tests__/`) remain; `npm run lint` green with the scope from 7a (destinations are covered, so every moved file is token-clean by construction).

PR 7d: `Refs #1471`; body lists each move as a table (from → to). Auto-merge, deploy.

---

## PR 7e — `components/session/`

### Task 1: transcript rendering → `web/src/features/sessions/transcript/`
`Turn`, `TranscriptView`, `transcriptRows.ts`, `BlockRow`, `AssistantText`, `UserBubble`, `Markdown`, `htmlComments.ts`, `ThinkingBlock`, `ToolCallCard`, `ToolGroup`, `ToolResult`, `BashResult`, `DiffView`, `FileContent`, `filePathRef.ts`, `CopyablePath`, `GenericResult`, `SearchResult`, `WebResult`, `StructuredView`, `ResultImages`, `SubagentResult`, `ErrorCard`, `NoticeCard`, `RecoveringCard`, `InterruptedAskNotice`, `CondensingProgress`, `ModelSwitchDivider`, `RotationDivider`, `InsightCallout`, `insights.ts`, `TodoList`, `SessionPlaceholder`. Emoji → lucide in `ToolCallCard` (`✓`→`Check`, `✗`→`X`) per R9.
### Task 2: composer + panels → `features/sessions/composer/` and `features/sessions/panels/`
`Composer`, `SessionComposer`, `MicButton`, `AttachmentChips`, `useImageAttachments`, `useSessionDraft`, `heicConvert`, `CommandPalette`, `CommandResultOverlay` → `composer/`; `PendingPermissionPanel`, `PendingQuestionPanel`, `PendingModelApprovalPanel`, `PendingPromptNote`, `YourTurnPanel`, `QuestionCard`, `QuestionGroup`, `PermissionCard`, `ModelSwitchDialog` → `panels/`. Emoji → lucide in `DispatchBar` was 7d; `Composer`'s attach/sparkle glyphs likewise here.
### Task 3: session chrome → `features/sessions/`
`SessionBody`, `SessionMeta` (uses `subAgentStatus.ts` from 7d Task 5), `SessionActions`, `SessionRuntimeProvider`, `SessionAriaLive`, `StatusBar`, `WorkingIndicator`, `workingWords.ts`, `brailleSpinner.ts`, `elapsed.ts`, `SubAgentList` (uses `subAgentStatus.ts`), `subAgentDuration.ts`, `SessionGoalText`, `ExportMenu`, `useCardKeyboard.ts`. Update the 4 `vi.mock('@/components/session/…')` paths (`AssistantSheet.test`, `MainPaneSession.actions.test` ×2, `DrawerHost.test`). `ReviewPage.tsx`/`SubAgentPage.tsx` imports move from `@/components/session/*` to the new paths.

Acceptance for 7e: `find web/src/components -type f` → empty; the directory itself is removed in 7f (so 7e's diff is moves only). `npm run lint` green.

---

## PR 7f — whole-tree lint, delete the bucket, docs, close the row

### Task 1: lint to the whole tree; delete `components/`
- `web/eslint.config.js`: `files: ['src/**/*.{ts,tsx}'], ignores: ['src/features/pipeline/charts/**']`; comment: "Token discipline covers the whole tree (row 7, 2026-09)."
- `git rm -r web/src/components` (should already be empty of files; remove the directory and any `__tests__` leftovers).
- `web/src/shell/useViewport.test.tsx` and `features/pipeline/charts/plotroom.test.ts` still pass (their walks tolerate the missing dir).
- Success greps (target-state §9 + this plan): `grep -rn "dark:" web/src --include=*.ts --include=*.tsx | grep -v index.css | grep -v "// " ` → 0; `grep -rnE "\b(bg|text|border)-(gray|blue|amber|red|green|emerald|violet|purple|indigo|sky|yellow|slate)-[0-9]" web/src | grep -v features/pipeline/charts` → 0; `grep -rn "min-h-\[44px\]\|z-50\b\|z-40\b\|z-30\b\|z-20\b\|z-10\b" web/src` → 0; `ls web/src/components` → "No such file".

### Task 2: `docs/ACCESSIBILITY.md`
Rewrite (page-migration §6): scope = every page and primitive; conformance target unchanged (WCAG 2.1 AA); sections: 1 Scope; 2 The shell (regions from `shell/AppShell.tsx` `REGION_IDS` with their landmark roles: `app.banners` = `BannerStack` live regions, `app.rail` = `NavRail`/`NavBar` nav, `app.list` = `ContextList` listbox, `app.main` = `PageHost` the ONE `main`, `app.dock` = `PromptDock` form, `app.drawer` = `DrawerHost` complementary, `app.overlays` = dialogs/toasts) and the four modes incl. kiosk (no dialogs, no actions, `--scale`); 3 The page contract (one `h1`, `document.title`, `PageContent` busy announcer, variants); 4 Primitives (`Dialog` focus trap, `Menu`, `Tabs`, `Banner` roles, `min-h-target` 44 px, `StatusPill` text twin for colour); 5 Tokens and contrast (`src/__tests__/contrast.test.ts`, the `-strong`/`on-strong` rule, `prefers-reduced-motion` via `useViewport().reducedMotion`); 6 Tests (axe in `shell/AppShell.test.tsx` with the `region` rule on, `pageContract.test.tsx`, Playwright per page); 7 Retired: the xterm terminal exception (no terminal surface exists). Keep the existing "Violations found and fixed" table as an appendix with its component names updated to the new paths.

### Task 3: `web/README.md`
Replace the Vite scaffold with: what the app is (one paragraph); folder map (`api/`, `session/`, `shell/`, `ui/`, `features/<domain>/`, `pipeline/`, `lib/`, `hooks/`, `e2e/`); the four gates (`typecheck`, `lint`, `test`, `build`) and `test:e2e`, plus `lint:fix-tokens`; conventions (tokens only, `ccswitch/no-raw-palette`, storage keys via `storageKey`, one media-query reader, lucide only); how to add a page (variant, `PageHeader`/`PageContent`, register in `navDefs`, add a `PAGES` row in `pageContract.test.tsx`, an e2e spec at three widths); how to add a primitive (`ui/`, test, no raw palette). Under 150 lines.

### Task 4: docs status cells + product doc
- `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` row 7 status → `landed <date> (Closes #1471; six PRs 7a–7f)`.
- `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` §3 PR 11 row → append `— landed <date> (finish-line row 7, #1471)`.
- `docs/superpowers/specs/2026-09-02-ui-target-state-design.md` §8 row 11 → same note; §9 first bullet gets `(met <date>)`.
- `docs/product/features/web-dashboard-mobile.md`: links to the moved files and the four modes (page-migration §6).

- [ ] Gate + Playwright; commit; PR `Fixes #1471`; auto-merge; deploy.

---

## Self-review

- **Spec coverage.** PR 11's list: `dark:` / raw palette / hex / emoji / `text-[Npx]` / `z-*` / `min-h-[44px]` (7a fixer + 7d/7e application + 7f whole-tree gate); `localStorage` namespace with one-time migration (7b); ESLint to repo scope (7a partial, 7f full); `docs/ACCESSIBILITY.md` and `web/README.md` (7f); `components/` empty (7d/7e/7f); `lib/useMediaQuery.ts` deleted (7c). Target-state §9: the two greps (7f Task 1), Playwright per route (R8: the existing page specs), build-output token test (pre-existing, unchanged). #1471 carry-overs: SubAgentStatus maps (7d Task 5 / 7e Task 3); EmptyState centring decided in the primitive (7d Task 4 folds `components/EmptyState` into `ui/EmptyState` — the implementer proposes a `fill` prop there if the folded component centred; ruling recorded then); title flicker and matchPath semantics stay open on the issue.
- **Placeholder scan.** The per-file class mapping inside 7d/7e is intentionally "run the fixer, hand-map residuals per §Mapping" — the mapping table IS the specification, and the rule reports every residual by name. The docs tasks specify section lists rather than prose. No TBDs.
- **Type consistency.** `rewriteClassString(text): { out, changed }` (7a) is the only new exported helper; `storageKey`/`migrateStorageKey` (7b) signatures are used verbatim in Task 2; `Viewport.reducedMotion` (7c) is the name `CardFrame`/`SessionCard` read; `features/sessions/subAgentStatus.ts` exports `STATUS_TONE`/`STATUS_LABEL` (7d Task 5) consumed in 7e Task 3.
