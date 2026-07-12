# Settings Tab Consolidation — Design

**Date:** 2026-07-12
**Status:** Approved (autonomous session — design decisions recorded here)

## Goal

Consolidate the top-level **Accounts**, **Automation**, and **Library** tabs and the
**Settings modal** into a single top-level **Settings** tab, in both the mobile
`Dashboard` and the wide-screen `DesktopShell`.

## Motivation

The tab strip has grown to seven top-level tabs; three of them (Accounts, Automation,
Library) are configuration/administration surfaces rather than day-to-day working
views, and a fourth configuration surface (the Settings modal) is hidden behind a gear
icon. Grouping all configuration under one Settings tab shortens the strip to five
tabs and gives settings a URL-addressable home.

## Approach

**Flattened sections.** The Settings tab hosts an inner section strip:

> General | Accounts | Automation | Plugins | Skills | Commands | MCP Servers

Section state lives in the `?section=` search param (default `general`), reusing the
exact pattern `LibraryView` already established. Search params are shell-independent,
so deep links work in both the routed mobile shell and the state-driven desktop shell.

### Alternatives rejected

- **Grouped sections (General | Accounts | Automation | Library, Library keeps its
  inner sub-tabs):** produces two stacked tab strips inside the Settings tab and a
  `?section=` param collision between the settings strip and LibraryView's own strip.
- **Settings tab + keep the modal:** two sources of truth for the settings form,
  violating constitution Principle VI. The modal must be purged in the same PR.

## Changes

### Tab model (`web/src/components/tabDefs.ts`, `TabId`)

- `TabId` = `'sessions' | 'pipeline' | 'archived' | 'insights' | 'settings'`.
- `TAB_DEFS` order: Sessions, Pipeline, Archived, Insights, Settings.
- `TabCounts`: drop `accounts`; rename `accountsAttention` → `settingsAttention`.
  The amber needs-reauth dot now decorates the Settings tab. The accounts count badge
  is dropped from the top level (the count is visible inside the Accounts section).
- `DesktopShell` has a duplicate local `TabId` type — update it identically.

### New `SettingsView` (`web/src/components/settings/SettingsView.tsx`)

- Renders the section strip (`role="tablist"`, same styling as LibraryView's strip)
  and mounts one section at a time:
  - `general` → `GeneralSection` (new — the old modal form, inline)
  - `accounts` → `AccountsManager` (unchanged)
  - `automation` → `AutomationView` (unchanged; `onOpenSession`/`onReviewSession`
    threaded through `SettingsView` props)
  - `plugins` / `skills` / `commands` / `mcp` → the four existing library section
    components, mounted directly.
- Section ids intentionally keep LibraryView's values (`plugins|skills|commands|mcp`)
  so old `/library?section=X` deep links redirect losslessly.

### `GeneralSection` (`web/src/components/settings/GeneralSection.tsx`)

- The `SettingsForm` body from `SettingsPanel.tsx` rendered inline: default tool
  approvals (+ full-auto warning), transcript display, repo clone root,
  notifications (`NotificationsToggle` reused as-is). Save button semantics kept;
  no overlay, focus trap, or Escape handling. The loaded/loading remount key trick
  is kept so the form initializes from server values.
- `SettingsPanel.tsx` (the modal) is **deleted**.

### `Dashboard` (mobile shell)

- Remove the accounts/automation/library tab panels; add a settings panel mounting
  `SettingsView` (own scroll region, like every sibling panel).
- The gear button in the Sessions header calls `setActiveTab('settings')` (which
  navigates to `/settings` in routed mode) instead of opening a modal.
- The controlled `dialog` union shrinks to `'new' | null`; all settings-dialog
  state and the `SettingsPanel` mount are removed.

### `DesktopShell`

- Header gear icon → `setTab('settings')`; `showSettings` state and the modal
  mount are removed.
- `mainPane` for `tab === 'settings'` mounts `SettingsView` with
  `onOpenSession={focusSession}`.

### Routing (`web/src/App.tsx`)

- Add `/settings` → `DashboardRoute tab="settings"`.
- Back-compat redirects (mobile routed shell):
  - `/accounts` → `/settings?section=accounts`
  - `/automation` → `/settings?section=automation`
  - `/library` → `/settings?section=<existing section param, default plugins>`
  - `?dialog=settings` on any dashboard route → redirect to `/settings` (the
    `dialog` param only accepts `new` now).

### Copy

- Toasts saying "check the Accounts tab" → "check Settings → Accounts" (both shells).

### Tests

- e2e `dashboard.spec.ts`: tab count 7 → 5; tab-name list updated; drop the
  `accounts (1)` badge assertion; keep the attention/badge pattern via sessions count.
- `App.dialogs.test.tsx`: `?dialog=settings` test becomes a redirect-to-`/settings`
  test.
- `SettingsPanel` unit tests → `GeneralSection` tests (same behaviors minus
  modal/focus-trap ones).
- `LibraryView.test.tsx` → `SettingsView` tests covering section switching.
- `DashboardSettings.test.tsx` updated: gear navigates to the settings tab.

## Error handling

No new failure modes: sections own their data hooks and error states already
(AccountsManager, AutomationView, library sections). GeneralSection inherits the
existing `useUpdateSettings` error path.

## Out of scope

- No backend/API changes.
- No changes to the section components' internals.
- The desktop shell remains state-driven (not URL-routed) apart from the shared
  `?section=` param.
