# Settings — Audit and Redesign

**Status:** proposed, awaiting review (autonomous session; decisions and the alternatives considered
are recorded in §4 so the reviewer can overturn any of them) · **Date:** 2026-09-04 · **Parent:**
`2026-09-02-ui-target-state-design.md` (umbrella, D11 "Settings = Split variant") and sub-spec C
row "PR 5 — Settings" · **Supersedes:** `2026-07-12-settings-tab-consolidation-design.md` (the
flat 7-tab strip that has since grown to 14) · **Evidence:**
`docs/audits/2026-09-04-settings-inventory.md` (every control, where it saves, what is missing),
`docs/audits/2026-09-04-settings-ui-standards-reference.md` (sourced industry expectations, with a
20-item checklist referenced below as **C1–C20**), and
`docs/superpowers/evidence/2026-09-04-settings-audit/` (accessibility-tree snapshots of all 14
sections plus three screenshots, taken from the live instance at `f92c6e34` at 1440 and 390 px).

## 1. Summary

Settings is where ccswitch's configuration surfaces went to live, not where they were designed to
live. The July consolidation folded a modal plus three tabs into one tab strip; seven more sections
have been appended since. The result today, measured against the reference checklist:

| Checklist item | Today |
|---|---|
| C1 ≤ ~5 top-level groups | 14 flat tabs; 5 of 14 visible on a phone, 9 of 14 at 1440 px, and the strip hides its own scrollbar, so the rest are undiscoverable |
| C3 preferences separated from admin | "General" mixes personal display preferences (transcript folding) with fleet-wide policy (permission mode, model gating, session caps, archive purge) |
| C6 one save model per form | General: explicit Save, except the push toggle inside it auto-saves. Accounts: an explicit Save form directly above an auto-saving select |
| C7/C8 visible saved / error feedback | `toast('Settings saved')`; a failed save shows nothing |
| C9 unsaved-changes guard | none; the tab strip discards edits silently |
| C15 destructive actions gated | 8 of 11 delete/remove/clear actions have no confirmation (channels, rules, hooks, hosts, presets, templates, profiles, quiet-hours clear); archive-path change silently moves every archive |
| C16 scope labelled | nothing says which settings are server-wide, which are this-browser (theme lives outside Settings entirely), or which are env-managed defaults ("Server default", "blank = default: fable") |
| C18 phone drill-down with Back | the same tab strip at every width; no section list, no Back, Settings itself is under "More" |
| C19 target size | Fleet's remove-host control is an unlabelled `✕` glyph |
| page contract (umbrella §7) | no `h1`, no `PageHeader`, `document.title` unset, section headings are `h2`/`h3`/none |

And three correctness defects hide behind the UI: the permission-mode select offers two values the
server silently drops; the "collapse thinking" default is `false` in the UI and `true` in the DB;
`SettingsView.test.tsx` still asserts seven tabs.

This spec (1) records the audit as numbered findings, (2) fixes the information architecture into
five groups with a vertical nav, (3) defines one chassis and one save contract every section uses,
(4) gates every destructive action, (5) labels scope, and (6) fixes the drift. It is the design PR
5 of the umbrella sequence executes.

## 2. Goals and non-goals

**Goals**

1. A person can find any setting in two moves from anywhere: Settings → group → section, with the
   nav visible at desktop and a list-then-detail drill-down on phone.
2. Every section is built from the same chassis and obeys the same save, error, and confirmation
   contract, so behaviour is predictable before reading a section.
3. Every setting says what it is (label + one-sentence description), where it applies (scope), and
   what the effective default is when blank.
4. No destructive or expensive action runs without a confirmation proportional to its reversibility.
5. Settings conforms to the umbrella page contract (§2.1 of sub-spec C) and closes audit F5.1 /
   F6.1 for this page.

**Non-goals**

- No new backend settings, except the small API additions in §9 (an `effectiveDefaults` block on
  `GET /api/settings`, and `max_sessions` on `PUT /fleet/hosts/{name}` which the model already has).
- No redesign of Automations (it leaves Settings; its own page is PR 6), the Library editors'
  dialogs, or the account add / reauth flows. They are re-hosted, not rewritten.
- No settings search in v1 (see D8).
- No per-session overrides for transcript display (sub-spec B already deferred this).

## 3. Audit findings

Severity: **H** blocks a task or hides data · **M** costs time or trust · **L** polish. Each finding
names the checklist item it fails and where the evidence is.

### 3.1 Information architecture and navigation

| # | Sev | Finding | Evidence |
|---|---|---|---|
| S1 | H | 14 flat tabs in a horizontally scrolling strip with a hidden scrollbar. 9 fit at 1440 px, 5 at 390 px; nothing indicates more exist. Presets, Templates, Profiles, Snapshots, Fleet are effectively hidden. | `SettingsView.tsx:32-47`; `ui/Tabs.tsx:38` (`overflow-x-auto [scrollbar-width:none]`); screenshots |
| S2 | H | Same layout at every width. Phone gets the strip, not a list → detail drill-down; no Back; Settings is under the bottom bar's More sheet. Fails umbrella §4.3 (Split variant) and C18. | `general-phone.png`; `NavBar.tsx:43` |
| S3 | M | No page title anywhere. No `h1`, no `PageHeader`, `document.title` stays "ccswitch". Section headings vary: `h2` uppercase ("ACCOUNTS"), `h3`, `h3` per group, or none (General). Fails the page contract. | a11y trees; audit F5.1 |
| S4 | M | Tab labels and content headings disagree: tab "Presets" → heading "Session Presets"; tab "Profiles" → "Permission profiles"; tab "Fleet" → "Fleet" but describes hosts. | `presets-desktop.png` |
| S5 | M | No section registry. Sections are an array literal plus a 14-branch conditional; nav, routing fallback and tests each re-derive the list. Umbrella §4.5 names `settingsDefs`. | `SettingsView.tsx:32-47, 82-95` |
| S6 | M | "General" is a dumping ground (C9 anti-pattern): permission policy, model gating, transcript display, two filesystem paths, archive policy, session cap and push notifications in one 44-px-input column with one Save at the bottom. | `GeneralSection.tsx` |
| S7 | M | Automation appears twice: top-level `/automations` and `/settings/automation`, two components (`AutomationsTab`, `AutomationView`). Fails C3 and constitution VI. Reassessed during implementation (R10): the two surfaces turned out to be status/actions (`AutomationsTab`: events, spawned sessions, manual dispatch) vs configuration (`AutomationView`: enable, mode, config form, schedule editor), not duplicates — so the section is kept until PR 6 folds configuration into the page. | `routes.tsx:243`; `SettingsView.tsx:84` |
| S8 | M | Theme (system / light / dark) is only in the rail menu. Umbrella D5 puts it in Settings → General too. | `GlobalActions.tsx:23-65` |
| S9 | L | Section order is accretion order, not frequency: Accounts (the most-visited, badge-bearing section) is second; Snapshots and Fleet, both operator tasks, are last and hidden. | `SECTIONS` |

### 3.2 Save semantics and feedback

| # | Sev | Finding | Evidence |
|---|---|---|---|
| S10 | H | Mixed save models inside one form (fails C6). General: one Save button, but the push "Enable" inside it applies immediately. Accounts: Nickname / reserve / extra-usage need Save; the "Model approval" select beneath them auto-saves. | `GeneralSection.tsx:311, 316`; `AccountsManager.tsx:363-382, 686-716` |
| S11 | H | No unsaved-changes guard (C9). Switching section, tab, or route discards General edits silently. | `SettingsView.tsx:76-78` |
| S12 | M | Save feedback is a toast only; there is no inline "Saved" and no `role="status"` (C7). A failed `PUT /api/settings` shows nothing at all (C8). | `GeneralSection.tsx:58` |
| S13 | M | Auto-saving controls give no feedback: channel/rule/hook/plugin enable checkboxes, fleet weight (on blur), per-account model approval. | inventory §1b, §1c, §1e, §2a |
| S14 | L | Fleet weight is an uncontrolled input saved on blur: a value typed and then navigated away from may or may not save. | `FleetSection.tsx:201-213` |

### 3.3 Destructive and expensive actions

| # | Sev | Finding | Evidence |
|---|---|---|---|
| S15 | H | Unconfirmed deletes (fails C15, WCAG 3.3.4): notification channels (also cascades to rules), notification rules, hooks, fleet hosts, presets, templates, permission profiles, quiet-hours Clear. Only account delete, snapshot restore, import apply and host update confirm. | inventory §1b–§1h |
| S16 | H | Changing "Archive storage path" moves every archived session on disk with no confirmation, no count, and failures are logged rather than surfaced; the DB row is written even when files did not move. | `server/api/settings.py:47-72, 139-143` |
| S17 | M | Selecting "Full auto" or "Bypass all permissions" shows an inline warning but saves like any other value. Given the warning's own text ("anyone who can reach this server can run code as you"), this deserves a confirmation on Save. | `GeneralSection.tsx:90-97` |
| S18 | M | The three confirmations that exist are `window.confirm`, which is unstyled, cannot show the affected sessions/files, and blocks the automation tools. | `SnapshotsSection.tsx:213, 338`; `FleetSection.tsx:171` |
| S19 | M | Fleet remove-host is an unlabelled `✕` text glyph, well under the 24 px minimum (C19). | `FleetSection.tsx:225-234`; `fleet-desktop.png` |

### 3.4 Control choice, labels, validation

| # | Sev | Finding | Evidence |
|---|---|---|---|
| S20 | H | Permission-mode drift: the UI offers `acceptEdits` and `bypassPermissions`; `_VALID_MODES` on the server accepts four values and silently ignores the other two. Saving "Bypass all permissions" leaves the previous mode in place with a "Settings saved" toast. | `permissionModes.ts:5-12`; `server/api/settings.py:19, 123` |
| S21 | M | Default drift: "Collapse thinking by default" initialises to `false` in the UI, the column default is `true`. | `GeneralSection.tsx:23`; `models.py:356` |
| S22 | M | Presets: "Permission mode" and "Account" are free-text fields; the first should be a select over `PERMISSION_MODES`, the second over real accounts. Profiles duplicates `PERMISSION_MODES` as a local `MODES` array with two values missing (parallel implementation). | `PresetsSection.tsx:70-76`; `PermissionProfilesSection.tsx:8` |
| S23 | M | Hand-rolled `role="switch"` in three places, `type="checkbox"` styled three ways, no `Switch`/`Checkbox`/`Radio` primitive. Switch is the right control for immediate on/off rows (C11); checkbox for saved forms. | `AutomationCard.tsx`, `PluginsSection.tsx`, `SessionMeta.tsx` |
| S24 | L | Missing descriptions: Tool calls default, Collapse thinking, Repo clone root have no help text; Weekly reserve % explains itself only in a `title` tooltip. | `GeneralSection.tsx:154-207`; `AccountsManager.tsx:326-344` |
| S25 | L | No input validation for URL (channels), comma lists (denylist), tool patterns (profiles), `{{variable}}` syntax (templates), rule priority (no bounds). | inventory §1b, §1f–§1h |
| S26 | L | Accounts' per-row actions are four icon-only buttons (power, pencil, trash, refresh) with no visible label; the destructive one sits between two safe ones. | `accounts-desktop.png` |

### 3.5 Scope, defaults, and hidden settings

| # | Sev | Finding | Evidence |
|---|---|---|---|
| S27 | M | Nothing states scope (C16). Server-wide policy, per-browser preferences and env-managed defaults look identical. | all sections |
| S28 | M | Effective defaults are hidden: "Server default (require approval)" and "blank = default: fable" are hard-coded strings, not the live values of `CCSWITCH_MODEL_APPROVAL_ACTION` / `_DENYLIST`; "Max active sessions (blank = default (5))" likewise. Repo root and archive home show no env default at all. | `GeneralSection.tsx:118, 132, 288` |
| S29 | M | Settings that exist in the API but nowhere in the UI: per-account `defaultModel`, `defaultPermissionMode`, `envOverrides`; fleet `max_sessions` (the file's own doc comment claims it is editable here); channel `kind`; editing existing channels/rules (only toggle + delete). | inventory §2a, §1e, §1b |
| S30 | L | Security-relevant server switches (`CCSWITCH_ALLOW_UNAUTHENTICATED`, `CCSWITCH_WEBAUTHN_ENABLED`, self-model-switch) and feature flags (adoption, Copilot, Xvfb) have no read-only indicator anywhere, so an operator cannot confirm the running posture from the UI. | inventory §4 |
| S31 | L | Per-browser preferences live under three localStorage namespaces (`ccswitch.`, `prdash.`, `workspace.`) and one uses a colon separator; pipeline notifications opt-in has no UI at all. | inventory §2f |

### 3.6 Consistency and page contract

| # | Sev | Finding | Evidence |
|---|---|---|---|
| S32 | M | Three visual idioms across sections: General uses raw `gray-*` / `dark:` classes and `min-h-[44px]`; Presets/Templates/Profiles use tokens; Library uses a third. Lint rule (umbrella §7) will fail General as-is. | `GeneralSection.tsx:61-67` |
| S33 | L | Empty states are bare paragraphs ("No presets yet…", "No channels yet."), not `EmptyState`. | inventory |
| S34 | L | `SettingsView.test.tsx` asserts "renders all seven section tabs". No tests exist for Presets, Templates or Profiles, the three sections with unconfirmed deletes. | inventory §5 |
| S35 | L | Stale docstring on `model_approval_action` (`approve\|deny` vs real `require_approval\|block\|allow`). | `models.py:383-385` |

## 4. Decisions (with alternatives)

| # | Question | Decision | Alternatives considered |
|---|---|---|---|
| D1 | Nav shape | **Grouped vertical `SettingsNav`** (five group headers, sections beneath), left of a scrolling content panel on desktop and tablet-landscape; on phone and tablet-portrait, `/settings` is the grouped list and each section is a page with Back (umbrella D11, C18). | Keep tabs but wrap (fails at 14 items); two-level tabs (audit rejected stacked strips in July); accordion (hides state, poor deep links). |
| D2 | Group set | **Workspace · Sessions · Accounts · Claude config · About** (§5). Five groups honours C1 while keeping 17 addressable sections (16 as first decided; the `automation` section was retained by R10). | Alphabetical flat list (no meaning); "Advanced" bucket (C9 anti-pattern). |
| D3 | Automation in Settings | **Keep** the section in 5a (group Sessions, label "Automations", last in group). Reversed during implementation: `/automations` has no configuration controls. PR 6 folds configuration into the Automations page and retires the section. | Originally decided (superseded): **remove** the section and redirect `/settings/automation` → `/automations`, for one surface (S7) — rejected once implementation showed it would delete the only automation-configuration UI. Also considered: keep both with a shared component (still two doors to one room). |
| D4 | Save contract | **Two models, never mixed in one section body**: `Switch` rows apply immediately with inline status; everything else is a form section with a sticky **Save / Discard** footer, dirty tracking, and a navigation guard (C6, C9). Rules in §7. | Auto-save everything (fails Primer's declarative-control rule and NN/g expectation); explicit save everywhere (toggles then need a Save, which no reference product does). |
| D5 | Feedback | **Inline** `role="status"` "Saved · just now" in the footer / row; errors as `Banner tone="danger"` at the top of the section with input preserved. No toast for settings saves (C7, C8). | Keep toast (not announced reliably; Primer advises against). |
| D6 | Confirmations | **`ConfirmDialog` primitive on the `Dialog` base**; every delete/remove/restore/import/clear uses it; irreversible cases (account delete, snapshot restore, archive-path move, import apply) require typing the item name (C15). The three `window.confirm` calls are replaced. | Undo-toast instead of confirm for deletes (needs server-side soft delete the API lacks). |
| D7 | Scope | **A `ScopeBadge` on every section header**: `Server` (all browsers, all hosts) · `This browser` · `Managed by env` (read-only rows). Effective defaults come from a new `effectiveDefaults` block on `GET /api/settings` (§9). | Per-row badges (noise); prose only (missed). |
| D8 | Search | **Not in v1.** With five groups and 17 sections every item is one scroll away in the nav; `settingsDefs` carries `keywords` so a filter box can be added later without a data change. | Ship a filter now (adds a third nav state to test on phone before the drill-down itself has shipped). |
| D9 | Theme | **Appearance section** under Workspace holds theme (System / Light / Dark radio, C2 wording), rail expanded, and the pipeline notifications opt-in; the rail menu stays as a shortcut and both write `ccswitch.theme` (umbrella D5, S8). | General only (buries a per-browser preference among server policy). |
| D10 | Danger placement | Each section's destructive actions sit in a **`Danger zone` group at the bottom of that section**, `Card tone="danger"` (GitHub/Vercel pattern), rather than one global danger page. | One "Danger" section (splits an entity from its own delete). |
| D11 | Primitives | Add **`Switch`, `Checkbox`, `RadioGroup`, `ConfirmDialog`, `SettingRow`, `SettingsSection`, `SaveBar`, `ScopeBadge`** to `web/src/ui` (sub-spec A owns the folder; PR 5a delivers them because Settings is their first consumer). | Build them inline in sections (repeats S23). |
| D12 | Sequence | **Five PRs** (§11) so each lands green in a day: chassis + nav + Workspace; Sessions; Accounts; Claude config; Notifications and cleanup. | One PR (touches 20+ files and every test). |

## 5. Information architecture

Five groups, seventeen sections. `id` is the URL segment and is stable; existing ids are kept so every
link in the wild still resolves. New ids are marked ★. Group order is frequency-first (C1
synthesis): what a person changes weekly is above what an operator changes yearly.

| Group | Section (id) | Scope | Contents (moved from) |
|---|---|---|---|
| **Workspace** | General (`general`) | Server | Repo clone root, archive storage path (General); auto-archive, max archived, max active (General) |
| | Appearance ★ (`appearance`) | This browser | Theme (rail menu); rail labels expanded; pipeline notifications opt-in (`prdash.notifications`); list/drawer widths shown read-only with Reset |
| | Notifications (`notifications`) | Server + this device | Push (this device), quiet hours, channels, rules. The duplicate push toggle leaves General |
| **Sessions** | Defaults ★ (`sessions`) | Server | Default tool approvals, model approval action + denylist, transcript display (General) |
| | Presets (`presets`) | Server | unchanged entity |
| | Templates (`templates`) | Server | unchanged entity |
| | Permission profiles (`profiles`) | Server | unchanged entity |
| | Automations (`automation`) | Server | `AutomationView` — enable, mode, config form, schedule editor; retained by R10 as the only automation-configuration UI, retired by PR 6 |
| **Accounts** | Accounts (`accounts`) | Server | `AccountsManager`, re-hosted on the chassis |
| | Hosts (`fleet`) | Server | Fleet, renamed to what it lists; gains max-sessions |
| **Claude config** | Plugins (`plugins`) · Skills (`skills`) · Commands (`commands`) · MCP servers (`mcp`) | Server (shared Claude config dir) | Library sections, re-hosted |
| | Hooks (`hooks`) | Server | unchanged |
| | Snapshots (`snapshots`) | Server | unchanged |
| **About** | About ★ (`about`) | — | Version / commit / host name, links (docs, changelog, help drawer), **Managed by env** table (S30): read-only rows for the flags in inventory §4 that shape behaviour, each showing the env var, the effective value, and "Set in `/etc/ccswitch/ccswitch.env`" |

Redirects: no new redirect out of Settings — `/settings/automation` is a real section (R10, D3).
Redirects kept: `/settings?section=`, `/library`, `/accounts`, `?dialog=settings`, and legacy
`/automation` → `/settings/automation`.

Nav badges: the rail's Settings dot (needs-reauth) is unchanged; inside Settings, the **Accounts**
nav item shows the count of accounts needing attention (C20, dot outside, count inside); a banner
that says "check Settings → Accounts" links to `/settings/accounts#<account>` and the row is
highlighted on arrival.

## 6. Layout

### 6.1 Page

`SettingsPage` (`web/src/features/settings/SettingsPage.tsx`), `data-page-variant="split"`:

```
<PageHeader title="Settings" />                              page.header, the only h1
<div class="flex flex-1 min-h-0">
  <SettingsNav />                                            nav, 240px, own scroll
  <PageContent busy={…}>                                     scrolls; max-w-3xl
    <SettingsSection …>                                      one per route
  </PageContent>
</div>
```

`document.title` = `"<Section> · Settings — ccswitch"`. Widths follow umbrella §4.1:

| Mode | Nav | Content |
|---|---|---|
| desktop ≥1024, tablet-landscape | vertical nav always visible; `aria-current="page"` on the active section | panel |
| tablet-portrait, phone | `/settings` renders the nav as a full-width grouped list (`role="list"`, disclosure chevrons, iOS idiom); `/settings/:section` renders the section with a `Back` in `PageHeader.actions` that navigates to `/settings` | full width |
| kiosk | Settings is `showInKiosk:false`; unchanged | — |

`/settings` on desktop redirects to the **last visited section** (`ccswitch.settings.last`, C1
"restore the most recently viewed pane") and to `general` when unset; on phone it is the list.

### 6.2 Nav

`SettingsNav` is derived from `settingsDefs.ts`:

```ts
export interface SettingsDef {
  id: SettingsSectionId; group: SettingsGroupId; label: string; icon: LucideIcon
  scope: 'server' | 'browser' | 'device' | 'none'
  description: string           // one sentence, shown under the section title
  keywords?: string[]           // for a future filter (D8)
  badge?: () => number | boolean | undefined
  component: React.LazyExoticComponent<React.ComponentType>
}
```

The route table, the nav, the phone list, the redirect fallback and `pageContract.test.tsx` all
read this array; adding a section without registering it fails the count check.

## 7. Section chassis and save contract

### 7.1 Chassis

```
<SettingsSection id title description scope>          h2 + description + ScopeBadge
  <SettingsGroup legend>                               <fieldset><legend>, optional
    <SettingRow label description htmlFor control />    label · description · control (right on ≥640px, below on phone)
  </SettingsGroup>
  <SettingsGroup legend="Danger zone" tone="danger" />  last, optional
  <SaveBar />                                          form sections only
</SettingsSection>
```

`SettingRow` is Microsoft's SettingsCard shape (C4): one control per row, description visible
(never tooltip-only, S24), and the control's `aria-describedby` points at the description. Rows
with an env-managed default show the effective default as the input placeholder and a `Reset to
default` link when the value differs (C12, S28).

### 7.2 Save models

| Model | Controls | Behaviour |
|---|---|---|
| **Immediate** | `Switch` rows only (enable/disable an entity, on/off policy) | `PATCH` on change; row shows `Saving…` → `Saved` (`role="status"`); on failure the switch reverts and a row-level error line appears; an `Undo` toast is offered for 6 s where the inverse is a single call |
| **Form** | text, number, select, checkbox, radio, textarea | Section keeps a draft; `SaveBar` (sticky bottom of the panel, `scroll-padding-bottom` so focus is never obscured, C19) shows `Save` / `Discard` enabled only when dirty and a `Saved · just now` status after success; errors as `Banner tone="danger"` at the section top, field errors under the field with an `Error:` prefix for AT (C8); `useBlocker` + `beforeunload` guard while dirty (C9) |

A section body contains **at most one** `SaveBar`, and `Switch` rows never sit inside the form's
fieldsets; they live in their own `SettingsGroup` above it, so the two models are visually and
semantically separated (Primer). Entity lists (channels, rules, hooks, hosts, presets, templates,
profiles, accounts) follow the same rule per card: an `enabled` switch is immediate, the edit form
is a form.

### 7.3 Confirmation tiers (D6)

| Tier | When | Dialog |
|---|---|---|
| **Confirm** | delete rule / hook / preset / template / profile; clear quiet hours; remove host; disable account; select `full-auto` or `bypassPermissions` on Save | `ConfirmDialog` with the item name in the title, one sentence of consequence, `Cancel` + destructive button |
| **Typed** | delete account; delete channel (cascades to N rules, count shown); restore snapshot; apply import; change archive storage path (shows the number of archives that will move) | same, plus `Type <name> to confirm` |
| **None** | re-enable, refresh, dry run, test routing, snapshot now, export | — |

### 7.4 Control rules (C11–C14)

- On/off that applies now → `Switch` (`role="switch"`, label names the on state, label never changes).
- On/off inside a saved form → `Checkbox`.
- 2–5 mutually exclusive values → `RadioGroup` (theme, model-approval action, tool-calls default).
- >5 values or space-constrained → `Select` (permission mode, hook event).
- Unavailable → disabled with a reason in the description, never hidden (C13): e.g. push on an
  unsupported browser, Copilot re-auth when `CCSWITCH_ENABLE_COPILOT` is off.

## 8. Per-section design

Only changes are listed; "re-hosted" means the existing component moves onto the chassis with no
behavioural change beyond the contract in §7.

| Section | Changes |
|---|---|
| **General** | Form. Groups: *Paths* (repo clone root with env default placeholder and description; archive storage path → Typed confirm on save when changed, shows archive count) · *Archive* (auto-archive `Checkbox`, max archived with "permanently purged" description, `Reset` (deferred from 5a; see §11)) · *Capacity* (max active; description names the queue). |
| **Appearance** ★ | Immediate rows: Theme `RadioGroup` (System / Light / Dark); Show navigation labels `Switch` (`ccswitch.railExpanded`); Pipeline notifications `Switch` (`prdash.notifications` → migrated to `ccswitch.pipelineNotifications`, S31). *Layout* group: list width / drawer width read-only with `Reset`. Scope badge `This browser`. |
| **Notifications** | *Push* group: `This device` badge; state text (enabled / unsupported / denied with the browser-settings hint) + `Switch`; Typed confirm not needed. *Quiet hours* form with `Clear` → Confirm. *Channels*: card per channel, `enabled` Switch immediate, **Edit** opens the same form as Add (S29), Delete → Typed (cascade count). *Rules*: same; priority `min=0 max=1000`; URL field `type="url"` with client validation (S25). Add forms become `Dialog`s opened from the group header's `+ Add` so the list is not pushed below a permanent form. |
| **Sessions › Defaults** ★ | Form. *Tool approvals*: permission mode `Select` over `PERMISSION_MODES`; **fix S20** by extending `_VALID_MODES` on the server to the six values `PERMISSION_MODES` defines (the server is the source of truth for the set; the client imports it, and a test asserts the two sets are equal); choosing `full-auto` / `bypassPermissions` → Confirm on Save, warning text retained. *Model approval*: action `RadioGroup` (Server default shows the effective env value), denylist input validated as a comma list against known tiers (deferred from 5a; see §11). *Transcript*: tool calls `RadioGroup`, collapse thinking `Checkbox` defaulting from the server payload, never a hard-coded `false` (S21). |
| **Presets** | Account → `Select` over `useAccounts()`; permission mode → `Select` over `PERMISSION_MODES` (S22); Delete → Confirm; empty → `EmptyState` with `New preset` action. |
| **Templates** | `{{variable}}` placeholders listed under the content field as parsed chips (validation feedback, S25); Delete → Confirm. |
| **Permission profiles** | Local `MODES` deleted, import `PERMISSION_MODES` (S22); patterns validated one-per-line non-empty; Delete → Confirm. |
| **Accounts** | Re-hosted. Card header: name + status pill; actions become a labelled `Menu` (Rename · Re-authenticate · Refresh · Disable/Enable · Delete) instead of four bare icons (S26). *Model approval* moves **into** the edit form beside reserve/extra-usage so the card has one save model (S10). Adds the three unexposed per-account fields to the edit form: default model, default permission mode, env overrides (key=value textarea; S29). Delete keeps its live-sessions list and becomes Typed. |
| **Hosts** (`fleet`) | Rows become `Card`s on phone, table on desktop. Weight becomes a controlled number field inside a per-host form with Save (S14); `enabled` stays an immediate `Switch`; **max sessions** input added (S29, model field exists); Remove → labelled danger button in the row's menu, Confirm (S19). `Update host` keeps its confirm, restyled. |
| **Plugins · Skills · Commands · MCP servers** | Re-hosted; plugin `enabled` becomes `Switch`; empties → `EmptyState`. |
| **Hooks** | Banner stays (`Banner tone="warning"`); enable → `Switch`; Delete → Confirm (shows the command); Add form → `Dialog`. |
| **Snapshots** | Restore / Apply import → Typed via `ConfirmDialog`, replacing `window.confirm` (S18); Export uses the api client's auth path, not a bare anchor. |
| **About** ★ | Read-only. Version, commit, host, uptime (deferred from 5a; see §11) from existing health endpoints; *Managed by env* table (S30) from `effectiveDefaults`; links (deferred from 5a; see §11). |

## 9. API changes (small, additive)

| Change | Why |
|---|---|
| `GET /api/settings` gains `effectiveDefaults: { repoRoot, archiveHome, modelApprovalAction, modelApprovalDenylist, maxActiveSessions }` and `managed: [{ key, value, source }]` for the flags in §5 About | S28, S30. Values are already in `get_settings()`; no secrets are included (the list is an allowlist in `server/api/settings.py`) |
| `_VALID_MODES` = the six `PERMISSION_MODES` values; 422 on unknown instead of silent skip | S20 |
| `PUT /api/settings` returns `409` with `{ detail: { code: 'archive_move_failed', moved, failed } }` when the archive move partially fails, and does not persist the new path in that case | S16 |
| `PUT /fleet/hosts/{name}` accepts `maxSessions` | S29 (column exists) |
| `PUT /notifications/channels/{id}` and `/rules/{id}` already accept full patches; no change | S29 |
| Docstring fix on `AppSettings.model_approval_action` | S35 |

## 10. Accessibility contract

- One `h1` ("Settings"), one `h2` per section, `legend` per group; `SettingsNav` is `<nav
  aria-label="Settings sections">` with `aria-current="page"`.
- Every control has a visible label and `aria-describedby` → description (+ error when present).
- Immediate rows and `SaveBar` announce through `role="status"`; errors through `Banner
  role="alert"`.
- `Switch` follows the APG switch pattern; `RadioGroup` is a native `fieldset` of radios.
- Targets ≥ 44 px (`min-h-target`); the sticky `SaveBar` sets `scroll-padding-bottom` on the panel
  (WCAG 2.4.11).
- `jest-axe` per section per mode (umbrella §7).

## 11. Sequence and deletions

| PR | Delivers | Deletes |
|---|---|---|
| **5a** | `settingsDefs`, `SettingsNav`, `SettingsPage` (Split, phone drill-down, last-section memory), primitives `Switch` `Checkbox` `RadioGroup` `ConfirmDialog` `SettingRow` `SettingsSection` `SettingsGroup` `SaveBar` `ScopeBadge`; **General**, **Sessions › Defaults** (the two halves of today's General form), **Appearance**, **About**; `effectiveDefaults`/`managed` API; `_VALID_MODES` fix + parity test; S21 default fix; redirects, with the `automation` section retained (R10); `pageContract` entry. Every other section is hosted unchanged inside the new page until its own PR | `SettingsView.tsx` and its tablist; `GeneralSection.tsx`; the theme-only `Menu` code path is kept but reads the same store |
| **5b** | **Presets**, **Templates**, **Permission profiles** on the chassis | local `MODES` in Profiles; free-text preset fields |
| **5c** | **Accounts**, **Hosts** | icon-only action row; uncontrolled weight input; `window.confirm` in Fleet |
| **5d** | **Plugins**, **Skills**, **Commands**, **MCP servers**, **Hooks**, **Snapshots** | `window.confirm` in Snapshots; bare export anchor |
| **5e** | **Notifications** (edit flows, dialogs, typed cascade delete); `prdash.notifications` migration; stale test names; `ACCESSIBILITY.md` settings section | duplicate push toggle in General; the 3 hand-rolled `role="switch"` sites migrate to `Switch` |

Each PR body cites the S-numbers it closes. After 5e: `grep -c 'window.confirm' web/src` → 0;
`grep -c "role=\"switch\"" web/src` → 1 (the primitive).

## 12. Tests

| Test | Asserts |
|---|---|
| `settingsDefs.test.ts` | ids unique and URL-safe; every def has description and scope; count equals the nav's rendered items and the route table's `:section` set |
| `SettingsPage.test.tsx` (× phone/tablet/desktop) | page contract; nav visible on desktop; `/settings` is a list on phone; Back returns to `/settings`; last-section redirect on desktop |
| `SaveBar.test.tsx` | disabled until dirty; Discard restores; blocker fires on navigation while dirty; status announced |
| `ConfirmDialog.test.tsx` | typed tier refuses until the name matches; focus trapped and restored |
| per-section tests | moved with the section; each destructive action has a "cancel → no request" and "confirm → request" pair; each immediate row has a "failure reverts" case |
| `permissionModes.parity.test.ts` | `PERMISSION_MODES` values ⊆ `/api/settings` accepted set (fetched from a fixture generated by the server test suite) |
| Playwright | `/settings/accounts` at 390 / 820 / 1440; delete-channel typed confirm; theme radio persists across reload |

## 13. Open questions for the reviewer

These were decided in D1–D12 without a live answer; each is cheap to reverse before PR 5a.

1. **Group names.** "Workspace / Sessions / Accounts / Claude config / About" — is "Claude config"
   the right name for the shared Claude config surfaces (plugins, skills, commands, MCP, hooks,
   snapshots), or should it match the July name "Library" plus Hooks/Snapshots?
2. **(superseded) Automation leaves Settings** (D3). Confirm nothing links to `/settings/automation` from docs
   or notifications that the redirect would not cover. **Resolved during implementation: section
   kept; see D3.**
3. **Typed confirmation for archive-path change** (D6): is a count-plus-typed dialog too heavy for
   a path you set once, or exactly right given it moves every archive?
4. **About › Managed by env** (S30): which env flags to show is an allowlist; the proposed set is
   inventory §4's "behave like settings" rows minus timing knobs. Trim or extend?
5. **Search** deferred (D8). If you expect >20 sections within a year, pulling it into 5a is
   cheaper than retrofitting the phone list.
