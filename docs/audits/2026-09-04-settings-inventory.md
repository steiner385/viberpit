# ccswitch Settings Inventory (audit evidence)

**Date:** 2026-09-04 · **Base:** `origin/master` @ `f92c6e34` · **Purpose:** the complete map of every
user-facing setting, where it lives, how it saves, and what is missing. Evidence for
`docs/superpowers/specs/2026-09-04-settings-redesign-design.md`. Line numbers are as of the base
commit.

## 0. How Settings is registered and routed

There is **no `settingsDefs.ts` and no `SettingsNav` component**. Sections are a hardcoded array
literal inside the view.

| Concern | Location | Notes |
|---|---|---|
| Section registry | `web/src/components/settings/SettingsView.tsx:32-47` (`SECTIONS`) | 14 entries; `SettingsSection` union at `:26-30`. Rendered as a flat `<Tabs>` strip (`:69-81`), then a chain of 14 `{section === 'x' && <XSection/>}` (`:82-95`) |
| Nav entry | `web/src/shell/navDefs.ts:108` | `id: 'settings'`, path `/settings`, badge key `'settings'`, `showInKiosk: false`, `hasList: false` |
| Path helper | `web/src/shell/navDefs.ts:143-146` (`settingsSectionPath`) | blank section → fallback (default `'general'`) |
| Canonical route | `web/src/shell/routes.tsx:249` | `/settings/:section` → `SettingsPage` (`:171-174`) |
| Index redirect | `web/src/shell/routes.tsx:215-217`, registered `:252` | `/settings` and `/settings?section=X` → `/settings/X` |
| Legacy `/library` | `web/src/shell/routes.tsx:205-212` | `/library?section=X` → `/settings/X`, fallback `plugins` |
| Legacy `?dialog=settings` | `web/src/shell/routes.tsx:228-231`, `:273` | any path + `?dialog=settings` → `/settings/general` |
| Legacy tab redirects | `web/src/shell/routes.tsx:256-257` | `/accounts` → `/settings/accounts`; `/automation` → `/settings/automation` |
| Dual-channel section state | `SettingsView.tsx:61-78` | prop (path) wins over `?section=`; the tab strip writes back through whichever channel it arrived on |

`SettingsView.test.tsx` asserts *"renders all seven section tabs"* while `SECTIONS` has **14**
entries: a stale test name/assumption.

## 1. Web Settings UI: every control

### 1a. General (`web/src/components/settings/GeneralSection.tsx`)

Single **explicit Save button** (`:315-319`) for the whole form; nothing auto-saves except the
embedded push toggle. Form is remounted via `key` when settings load (`:17`).

| Setting | Location | Control | API field / persisted where | Notes |
|---|---|---|---|---|
| Default tool approvals | `:74-89` | select (6 opts from `web/src/session/permissionModes.ts:5-12`) | `defaultPermissionMode` → `app_settings.default_permission_mode` | Save button. **Danger:** conditional `role="alert"` warning shown only for `full-auto` (`:90-97`). **Validation mismatch:** UI offers `acceptEdits` + `bypassPermissions`, but backend `_VALID_MODES` (`server/api/settings.py:19`) only accepts `interactive/auto/allowlist/full-auto`; the two extra values are silently dropped |
| Action for gated models | `:106-122` | select: `''` (server default) / `require_approval` / `block` / `allow` | `modelApprovalAction` → `app_settings.model_approval_action` | Help text `:123-125`. `''` → `null`. Backend validates against `VALID_ACTIONS`, 422 otherwise (`server/api/settings.py:160-163`) |
| Approval denylist | `:134-141` | text, placeholder `fable` | `modelApprovalDenylist` → `app_settings.model_approval_denylist` | Help text `:142-144`. Free-text comma list, **no client validation** |
| Tool calls default | `:154-169` | select: `auto` / `all-open` / `all-closed` | `defaultToolDetail` → `app_settings.default_tool_detail` | No help text |
| Collapse thinking by default | `:172-189` | checkbox | `thinkingCollapsed` → `app_settings.thinking_collapsed` | No help text. **Default skew:** UI falls back to `false` (`:23`), DB column default is `True` (`server/db/models.py:356`) |
| Repo clone root | `:200-207` | text | `repoRoot` → `app_settings.repo_root` | **No help text.** No path validation |
| Archive storage path | `:218-225` | text | `archiveHome` → `app_settings.archive_home` | Help text `:226-229`. **Side effect:** changing it triggers a filesystem migration of every archived session (`server/api/settings.py:139-143`, `_migrate_archive_home` `:47-72`), **no confirm dialog** |
| Auto-archive completed sessions | `:235-251` | checkbox | `autoArchiveEnabled` → `app_settings.auto_archive_enabled` | Help text `:252-255`. Defaults to `true` (NULL surfaces as enabled) |
| Max archived sessions | `:266-276` | number, `min=1 step=1` | `maxArchivedSessions` | Help text warns of **permanent purge**. Blank/≤0 → `0` = unlimited. Server clamps to 100 000 |
| Max active sessions | `:290-300` | number, `min=1 step=1` | `maxActiveSessions` | Blank → `null`. Server clamps `max(1, …)`; raising the cap drains the queue (`settings.py:144-158`). Default 5 |
| Phone notifications | `:311` → `NotificationsToggle` | Enable/Disable link button | Browser push subscription (`/api/push/subscribe`) | **Auto-saves immediately**, not covered by the Save button. Duplicated in the Notifications section |

Confirmation feedback: `toast('Settings saved')` (`:58`). No error surface: a failed PUT shows
nothing.

### 1b. Notifications (`web/src/components/settings/NotificationsSection.tsx`)

| Setting | Location | Control | API field | Notes |
|---|---|---|---|---|
| Phone notifications | `:98-99` | Enable/Disable | push subscription | Same `NotificationsToggle` as General: **duplicated surface** |
| Quiet hours start / end | `:152-169` | `input type="time"` ×2 | `quiet_hours_start/end` | Explicit **Save** (`:171`); **Clear** (`:174-176`) PUTs nulls, no confirm |
| Channel enabled | `:225-231` | checkbox per channel | `PUT /notifications/channels/{id}` | **Auto-save on change** |
| Channel delete | `:234-241` | red "Delete" link | `DELETE /notifications/channels/{id}` | **Destructive, NO confirm.** Invalidates rules too |
| Add channel: Name, URL, Headers | `:296-321` | text, text, JSON textarea | `name`, `url`, `headers` | Submit disabled until name+url non-blank. **No URL-format validation.** JSON validated client-side with two distinct errors |
| Rule enabled | `:398-405` | checkbox per rule | `PUT /notifications/rules/{id}` | **Auto-save** |
| Rule delete | `:407-414` | red "Delete" link | `DELETE /notifications/rules/{id}` | **Destructive, NO confirm** |
| Add rule: Event, Action, Channel, Repo/Account/Automation scope, Critical, Priority | `:500-555` | select ×3 (channel conditional), text ×3, checkbox, number | rule fields | Priority has **no min/max**; lower = evaluated first |
| Test routing | `:562-564` | button | `POST /notifications/test` | Dry-run; renders decision panel |

No editing of existing channels/rules (only enable-toggle and delete) although `ChannelPatch` /
`RulePatch` support full updates (`server/api/notifications.py:142-148, 247-257`).

### 1c. Hooks (`web/src/components/settings/HooksSection.tsx`)

Every mutation is a full-array `PUT /hooks` (read-modify-write).

| Setting | Location | Control | Notes |
|---|---|---|---|
| Danger banner | `:147-159` | `role="alert"` amber banner | server-supplied `warning` with hardcoded fallback |
| Hook enabled | `:192-202` | checkbox per row | **Auto-save** |
| Dry run | `:212-220` | button per row | `POST /hooks/dry-run`; panel shows exit code / stdout / stderr |
| Hook delete | `:221-229` | `variant="danger"` button | **Destructive, NO confirm** |
| Add hook: Event, Matcher, Command | `:255-298` | select (9 events), text, text | Validation: `'Command is required'`. Form state preserved on failed PUT |

### 1d. Snapshots (`web/src/components/settings/SnapshotsSection.tsx`)

The only section with real confirm gates.

| Setting | Location | Control | Notes |
|---|---|---|---|
| Snapshot label + Snapshot now | `:128-151` | text + primary button | `POST /snapshots` |
| Export bundle | `:152-161` | `<a download>` to `/api/snapshots/export` | Plain anchor, not the api client |
| Diff | `:234-241` | button per snapshot | added/removed/modified counts; expandable file list |
| Restore | `:243-248` | danger button per snapshot | **`window.confirm` gate** (`:213-218`) |
| Import file picker + Apply import | `:350-402` | `input type="file"` + danger button | Auto-previews on select; **`window.confirm` gate** (`:338-344`) |

### 1e. Fleet (`web/src/components/settings/FleetSection.tsx`)

| Setting | Location | Control | Notes |
|---|---|---|---|
| Host weight | `:201-213` | number `min=1 max=1000`, uncontrolled, `onBlur` | **Auto-save on blur** |
| Host enabled | `:215-222` | checkbox | **Auto-save** |
| Remove host | `:225-234` | `✕` button | **Destructive, NO confirm** |
| Update host | `:166-186` | conditional inline button | **`window.confirm` gate** ("Its daemon will restart") |
| Add host: Name, Endpoint | `:252-273` | text ×2 | Submit disabled until both non-blank |
| Max sessions | *displayed only* (`:27-30`) | — | Doc comment `:32-33` claims it is tunable here; **no input exists** |

### 1f. Presets (`PresetsSection.tsx`)

Create/edit form driven by a field-descriptor array (`:70-76`), all plain text inputs.

| Setting | Control | Notes |
|---|---|---|
| Name * | text | Only validation: Save disabled unless non-blank |
| Repo | text `owner/name` | |
| Account | text | **Not a select of real accounts** |
| Model | text | Free text |
| Permission mode | **text**, placeholder "interactive / auto / full-auto" | **Should be a select**; no validation against `PERMISSION_MODES` |
| Initial prompt | textarea | |
| Save / Cancel | buttons | Explicit save |
| Delete | red button per preset | **Destructive, NO confirm** |

### 1g. Templates (`TemplatesSection.tsx`)

Name * (text) · Content (textarea, monospace, `{{variable}}` syntax documented, **not validated**)
· Tags (comma text) · Save (explicit) · Delete (**NO confirm**).

### 1h. Profiles (`PermissionProfilesSection.tsx`)

Name * · Description · Permission mode (select over a **local `MODES` array that duplicates
`PERMISSION_MODES`** and omits two values) · Tool patterns (textarea, one per line, **not
validated**) · Save (explicit) · Delete (**NO confirm**).

### 1i. Sections delegated to non-settings components

`accounts` → `AccountsManager`; `automation` → `AutomationView`; `plugins`/`skills`/`commands`/`mcp`
→ `web/src/components/library/*`. None follow the Settings visual conventions: `GeneralSection`
uses gray-scale Tailwind with `min-h-[44px]`; Presets/Templates/Profiles use `border-edge` /
`bg-accent` semantic tokens; Library uses a third style.

## 2. Settings-like surfaces outside the Settings sections

### 2a. Accounts (`web/src/components/AccountsManager.tsx`)

| Setting | Location | Control | Notes |
|---|---|---|---|
| Nickname | `:311-324` | text (edit mode) | Enter saves / Escape cancels |
| Weekly reserve % | `:326-344` | number 1–100 | Help via `title=` tooltip only |
| Allow extra usage credits | `:346-362` | checkbox (edit mode only) | Behind the edit form because "opting in spends real money" (`:344-346`) |
| Save / Cancel | `:363-382` | buttons | Explicit save for the three fields above |
| Model approval (per account) | `:686-716` | select: Inherit / require / block / allow | **Auto-saves on change**: inconsistent with the fields above |
| Enable / Disable account | `:403-419` | power icon toggle | Auto-save |
| Rename / Delete / Refresh | `:421-460` | icon buttons | Delete **gated by confirm dialog** listing live sessions |
| Re-authenticate · Copilot device flow · Generate long-lived token | `:509-580` | buttons | Multi-step flows |
| Add account | `:801-812` | button → `AddAccountDialog` | |
| Priority / rank | `:49-71` | read-only `#1 next` chip | Not editable |

Per-account fields the API accepts but **no UI surfaces** (`server/api/accounts.py:355-362`):
`defaultModel`, `defaultPermissionMode`, `envOverrides`.

### 2b. Automation (`AutomationView.tsx` / `AutomationCard.tsx` / `ScheduleEditor.tsx`)

| Setting | Location | Control | Notes |
|---|---|---|---|
| Automation enabled | `AutomationCard.tsx:282-303` | `role="switch"` | **Auto-save**; enabling auto-picks a non-`off` mode when the mode select is hidden |
| Mode | `:267-281` | select | Auto-save. Rendered **only when `modes.length > 2`** |
| Config (dynamic) | `:105-145`, `:186-210` | schema-driven bool/int/string/enum | Per-field help when present |
| Schedule preset / active hours / days / cron | `ScheduleEditor.tsx:205-281` | select, time ×2, 7 toggle buttons, text | Explicit **Save schedule** |

### 2c. Library (Plugins / Skills / Commands / MCP)

Plugin enabled (checkbox, auto-save) · Skill create/edit + AI draft (dialog, textarea + Generate) ·
Command create/edit (dialog) · MCP server add/edit (dialog; OAuth via `McpAuthFlow`).

### 2d. Theme (`web/src/shell/theme.tsx`, `GlobalActions.tsx`)

Icon button → menu (system/light/dark). Persisted `localStorage['ccswitch.theme']`. **Not in the
Settings section at all.** `?theme=` overrides per load; kiosk pins it.

### 2e. Per-session settings (session view)

Account switcher · Model switcher · Plan mode switch · Conversation/Browser tab. Tool detail and
thinking collapse have **no per-session control** (global only). `autoArchiveOptOut` is exposed
read-only in the API with no UI.

### 2f. Every `localStorage` key

| Key | Location | Stores |
|---|---|---|
| `ccswitch.theme` | `shell/theme.tsx:5` | `'light'`/`'dark'`; absent = system |
| `ccswitch.listWidth` | `lib/sidebarWidth.ts:16` | sidebar width |
| `ccswitch.sidebarWidth` | `lib/sidebarWidth.ts:18` | legacy, migrated then removed |
| `ccswitch.drawerWidth` | `shell/drawerWidth.ts:23` | drawer width |
| `ccswitch.railExpanded` | `shell/regions/NavRail.tsx:28` | rail expanded |
| `ccswitch.lastRepo` | `lib/lastRepo.ts:7` | last-used repo |
| `ccswitch.sessionOrder[.ns]` | `lib/sessionOrder.ts:19-20` | sort/filter per namespace |
| `ccswitch.groupCollapse[.ns]` | `lib/groupCollapse.ts:6-7` | collapsed group ids |
| `ccswitch.deployFailureDismissed` | `components/DeployPanel.tsx:15` | dismissed signature |
| `ccswitch:draft:{sessionId}` | `components/session/useSessionDraft.ts:7` | composer draft (**colon** separator) |
| `prdash.notifications` | `pipeline/useDashboard.ts:12` | pipeline notification opt-in (**`prdash.` namespace, no Settings UI**) |
| `prdash.collapsed` | `pipeline/PipelineView.tsx:31` | collapsed repos (stale namespace) |
| `workspace.focusedPipeline` | `pipeline/PipelineSwitcher.tsx:10` | focused pipeline (**third namespace**) |

## 3. Backend settings model

### 3a. `AppSettings` (`server/db/models.py:349-388`, table `app_settings`, singleton)

| Column | Type | Default | Exposed as | In UI? |
|---|---|---|---|---|
| `default_permission_mode` | String NOT NULL | `"interactive"` | `defaultPermissionMode` | General |
| `thinking_collapsed` | Bool NOT NULL | `True` | `thinkingCollapsed` | General (UI fallback `false`) |
| `repo_root` | String NULL | `None` | `repoRoot` | General |
| `default_tool_detail` | String NULL | `None` → `"auto"` | `defaultToolDetail` | General |
| `compaction_ewma_ms`, `compaction_samples` | Float/Int NULL | `None` | — | not exposed (telemetry) |
| `quiet_hours_start/end` | String NULL `"HH:MM"` | `None` | `/notifications/quiet-hours` | Notifications |
| `max_archived_sessions` | Int NULL | `None` = unlimited | `maxArchivedSessions` | General |
| `archive_home` | String NULL | `None` | `archiveHome` | General |
| `max_active_sessions` | Int NULL | `None` → 5 | `maxActiveSessions` | General |
| `auto_archive_enabled` | Bool NULL (tri-state) | `None` = enabled | `autoArchiveEnabled` | General |
| `model_approval_action` | String NULL | `None` | `modelApprovalAction` | General |
| `model_approval_denylist` | String NULL | `None` | `modelApprovalDenylist` | General |

Router `server/api/settings.py`: `GET`/`PUT /api/settings`. `SettingsBody` fields are all optional;
`None` = unchanged except `modelApprovalAction`/`modelApprovalDenylist` (explicit `null` clears via
`model_fields_set`). Stale docstring at `models.py:383-385` says values are `approve|deny`; real
values are `require_approval|block|allow`.

### 3b–3e. Other settings-bearing tables

| Table | Location | Settings fields |
|---|---|---|
| `NotificationChannel` | `models.py:412-431` | name, kind (`webhook` only; patchable, no UI), url, headers, enabled |
| `NotificationRule` | `models.py:433-454` | enabled, event_class, scope_repo/account/automation, action, channel_id, critical, priority |
| `Automation` | `models.py:456-489` | enabled, mode, config, schedule |
| `SessionPreset` | `models.py:802-823` | name, repo, account, model, permissionMode, initialPrompt |
| `PromptTemplate` | `models.py:825-842` | name, content, tags |
| `PermissionProfile` | `models.py:844-861` | name, description, mode, patterns |
| `Host` (fleet) | `models.py:863-906` | enabled, weight, max_sessions, endpoint, ssh_host, repo_path |
| `RepoDeployConfig` | `models.py:783-800` | per-repo deploy config |
| `PushSubscription` | `models.py:278-289` | per-device push endpoint/keys |

### 3f. Per-account settings (file-backed ledger, `src/ccswitch/accounts.py`)

| Field | UI |
|---|---|
| `nickname`, `weeklyReservePct`, `allowExtraUsage`, `enabled`, `model_approval_action` | yes |
| `default_model`, `default_permission_mode`, `env_overrides` | **NO UI** |

### 3g. `/api/config` (`server/api/config.py`)

Returns `{repoRoot, adoptionEnabled}`; `adoptionEnabled` (`CCSWITCH_ENABLE_ADOPTION`) gates the
Adopt UI and is not editable anywhere.

## 4. Environment variables that behave like settings

About 170 distinct `CCSWITCH_*` names exist. The settings-relevant subset:

**Direct overlaps with UI settings:** `CCSWITCH_REPO_ROOT`, `CCSWITCH_ARCHIVE_HOME`,
`CCSWITCH_MODEL_APPROVAL_ACTION` (default `require_approval`), `CCSWITCH_MODEL_APPROVAL_DENYLIST`
(default `fable`), `CCSWITCH_FLEET_HOSTS`. The UI shows "Server default" / "blank = default" without
saying what the server default currently is.

**Behave like settings, no UI:** `CCSWITCH_SELF_MODEL_SWITCH_ENABLED`, `CCSWITCH_SELF_MODEL_SWITCH_CAP`,
`CCSWITCH_AUTO_RESUME_ON_RESTART`, `CCSWITCH_ENABLE_ADOPTION`, `CCSWITCH_ENABLE_COPILOT`,
`CCSWITCH_ENABLE_XVFB`, `CCSWITCH_GH_RECONCILE_ENABLED`, `CCSWITCH_CLIENT_LOG_LEVEL`,
`CCSWITCH_LOG_LEVEL`, `CCSWITCH_SUGGESTIONS`/`SUGGEST_MODEL`, `CCSWITCH_SUMMARIES`/`SUMMARY_MODEL`,
`CCSWITCH_CONFIG_BACKUP_RETENTION_S`, `CCSWITCH_ALERT_WEBHOOK`, plus browser/VNC, Copilot proxy,
reauth, timing, and disk/memory threshold knobs.

**Security switches with no UI indicator of their state:** `CCSWITCH_ALLOW_UNAUTHENTICATED`,
`CCSWITCH_WEBAUTHN_ENABLED`.

**Infra-only (correctly not settings):** DB URL, host/port, secrets, allowlists, sessiond TLS,
deploy, GitHub App, daemon, test/fake flags.

## 5. Tests covering settings UI

| Test file | Asserts |
|---|---|
| `settings/__tests__/SettingsView.test.tsx` | section routing, path-vs-query precedence; **stale "seven section tabs"** |
| `settings/__tests__/GeneralSection.test.tsx` | prefill, full-auto warning, save payload, blank→0, auto-archive default |
| `settings/__tests__/ModelApprovalControls.test.tsx` | global + per-account model approval |
| `settings/__tests__/FleetSection.test.tsx` | host list, probe error, capacity, Update button visibility, confirm gating |
| `settings/__tests__/NotificationsToggle.test.tsx` | unsupported message; subscribe on enable |
| `settings/HooksSection.test.tsx` | banner, add, toggle, dry-run, 422 inline |
| `settings/NotificationsSection.test.tsx` | quiet hours, channel POST, bad-JSON error, rule channelId, test routing |
| `settings/SnapshotsSection.test.tsx` | list, snapshot now, diff, restore confirm / cancel, import preview |
| `api/__tests__/hooks.settings.test.ts` | `/settings` path (double-prefix guard), invalidation |
| `shell/routes.test.tsx`, `shell/navDefs.test.ts` | redirects, `settingsSectionPath` |
| `shell/regions/NavRail.test.tsx:180` | theme menu persistence |
| `components/__tests__/Accounts*.test.tsx` (8) | per-account controls |
| `components/__tests__/automationTab`, `AutomationCard`, `ScheduleEditor` | automation controls |
| `components/library/*.test.tsx` | library toggle/dialog behaviour |

**Coverage gaps:** no tests for `PresetsSection`, `TemplatesSection`, `PermissionProfilesSection`,
the three sections with unconfirmed destructive deletes.

## 6. Known caveats encoded in comments

| Note | Location |
|---|---|
| `?section=` kept working "until the Settings migration"; migration incomplete | `web/src/shell/routes.tsx:167-169` |
| Archive-home migration failures are logged, never raised: the DB row writes even when files did not move; edge-side `shutil.move` no-ops on split hosts (#709) | `server/api/settings.py:47-72` |
| Stale docstring on `model_approval_action` values | `server/db/models.py:383-385` |
| `allow_extra_usage` behind the edit form because it spends real money | `AccountsManager.tsx:344-346` |
| 2-mode automations hide the mode select | `AutomationCard.tsx:288-299` |
| Fleet doc comment claims max-sessions is tunable here; no input | `FleetSection.tsx:32-33` |
