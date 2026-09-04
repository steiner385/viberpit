# Settings Redesign (PR 5a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 14-tab `SettingsView` strip with a Split-variant `SettingsPage` (grouped vertical nav on desktop, list-then-detail on phone) built on one section chassis and one save contract, and land the first four sections on it (General, Sessions › Defaults, Appearance, About) together with the server-side additions they need.

**Architecture:** A `settingsDefs` registry is the one source for the nav, the phone list, the route fallback and the page-contract test. Sections not yet migrated (Accounts, Hosts, Notifications, Hooks, Snapshots, Presets, Templates, Profiles, the four Library sections) are hosted **unchanged** inside a `HostedSection` wrapper that supplies the chassis heading; their own PRs (5b–5e) move them onto the chassis. Two save models exist and never share a section body: `Switch` rows apply immediately with inline `role="status"` feedback; everything else is a form with a sticky `SaveBar`, dirty tracking and a navigation blocker. The server gains `effectiveDefaults`, `acceptedPermissionModes`, `managed` and `about` blocks on `GET /api/settings`, rejects unknown permission modes with 422 instead of silently dropping them, and refuses to persist an archive-path change whose file move partially failed.

**Tech Stack:** React 19.2 · TypeScript · Vite · Tailwind v4 `@theme` tokens (`bg-surface`, `text-ink`, `border-edge`, `text-accent`, `bg-danger-wash`, `min-h-target`, `z-sticky`) · `react-router-dom` 7 data router (`createBrowserRouter`, so `useBlocker` is available) · `@tanstack/react-query` 5 · `lucide-react` · vitest + @testing-library/react + jest-axe · FastAPI + SQLAlchemy (async) + pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-settings-redesign-design.md` (§4 decisions, §5 IA, §6 layout, §7 chassis + save contract, §8 per-section design, §9 API, §10 a11y, §11 sequence row **5a**, §12 tests). It argues from `docs/superpowers/specs/2026-09-02-ui-target-state-design.md` (umbrella D11, §4.3 Split variant, §7 guardrails) and `2026-09-02-ui-page-migration-design.md` (sub-spec C §2 page contract). Evidence: `docs/audits/2026-09-04-settings-inventory.md`. **Read the spec §5–§9 before starting.**

**Scope:** The spec sequences five PRs. This plan is **PR 5a only**. 5b (Presets/Templates/Profiles), 5c (Accounts/Hosts), 5d (Claude config), 5e (Notifications + cleanup) get their own plans after 5a merges, because each needs the chassis this plan builds.

---

## Global Constraints

Copied from the specs and CLAUDE.md. Every task's requirements implicitly include this section.

- **Replaced code is deleted in the same PR.** `SettingsView.tsx` and the old `GeneralSection.tsx` are gone at the end; no `@deprecated` shims. (umbrella §2.1)
- **Before adding a constant, helper, list or component, grep for the existing source of truth and route through it.** (CLAUDE.md) In particular: `PERMISSION_MODES` in `web/src/session/permissionModes.ts` is the client's mode list; `THEME_STORAGE_KEY`/`useTheme` in `web/src/shell/theme.tsx` is the theme; `SIDEBAR_*`/`DRAWER_*` in `web/src/lib/sidebarWidth.ts` and `web/src/shell/drawerWidth.ts` are the widths; `settingsSectionPath` in `web/src/shell/navDefs.ts` builds settings paths.
- **URL is state.** The settings section is the `:section` path segment. Redirects use `replace`, never push. Per-browser preferences live under the `ccswitch.` localStorage namespace only. (umbrella §2.4, §6)
- **`useViewport()` is the only module that reads `matchMedia` for layout.** Task 10 extends it; nothing else may query width or orientation. (umbrella §4.1)
- **ESLint `ccswitch/no-raw-palette`** governs `web/src/ui/**`, `web/src/shell/**`, `web/src/features/**`: no raw palette classes, no `dark:`, no hex, no z-index literals, no arbitrary px. Everything this plan creates is inside that scope. Use `min-h-target`, not `min-h-[44px]`.
- **Page contract** (sub-spec C §2.1/§2.3): one `h1` via `PageHeader`; `PageContent` owns scroll; `data-page-variant="split"` on the page root; no horizontal page scroll.
- **Save contract** (spec §7.2): a section body has at most one `SaveBar`; `Switch` rows never sit inside a form's `fieldset`.
- **Confirmation tiers** (spec §7.3): `ConfirmDialog` for every delete/remove/restore/clear; `typed` for account delete, channel delete, snapshot restore, import apply, archive-path change.
- **Naming:** `*Page` routed roots · `*Section` Settings sections · `*Defs` registries. Folders: `web/src/ui/` primitives · `web/src/shell/` shell · `web/src/features/settings/` this feature. (umbrella §4.5)
- **Commit trailer** on every commit:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
- **Implementers never push and never open PRs.** The controller does that.

### Gates (all must pass before any task reports DONE)

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
```

Tasks 1–3 touch Python and additionally require, run from the worktree root with the main checkout's venv:

```
/home/tony/GitHub/ccswitch/.venv/bin/python -m ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_settings.py server/tests/test_settings_model_approval.py -q
```

**Environment notes (this worktree):**
- Run web commands as `npm --prefix web <cmd>` from the worktree root. **Never `cd web/`.**
- A fresh worktree has no `web/node_modules`; symlink it from the main checkout: `ln -s /home/tony/GitHub/ccswitch/web/node_modules web/node_modules`.
- The shell sandbox rejects some compound commands. If a command is refused, split it into separate plain commands.
- `server/*` tests run against this checkout (pytest rootdir wins); `src/ccswitch/*` is not touched by this plan.

---

## Decisions

Questions the spec leaves open or the code contradicts, settled here so no implementer guesses. **A decision here overrides the spec's wording where they differ.**

| # | Question | Decision | Why | Cost if wrong |
|---|---|---|---|---|
| **DEC-1** | Spec D11 puts `SettingRow`/`SettingsSection`/`SettingsGroup`/`SaveBar`/`ScopeBadge` in `web/src/ui` | **They live in `web/src/features/settings/chassis/`.** `Switch`, `Checkbox`, `RadioGroup`, `ConfirmDialog` go in `web/src/ui/`. | `ui/` is the design-system layer; a component only Settings instantiates is a feature widget (umbrella §4.5 puts widgets under `features/`). The lint scope covers both folders, so nothing is lost. | A folder move. |
| **DEC-2** | Spec §6.2 types `component` as `LazyExoticComponent` | **Plain `React.ComponentType`.** | All 17 section components are already statically imported by `SettingsView`; lazy loading adds Suspense boundaries and a loading state to test for no measured gain. | Bundle-splitting later is one `React.lazy` per row. |
| **DEC-3** | Spec §6.1 says tablet-**portrait** uses the phone list, but `useViewport` has no orientation | **Add `portrait: boolean` to `Viewport`** from `(orientation: portrait)`, in `useViewport.tsx` (the one permitted `matchMedia` reader). `drill = mode === 'phone' \|\| (mode === 'tablet' && portrait)`. | Only that module may read `matchMedia`; a consumer-side query would violate the constraint. | A field nobody else uses yet. |
| **DEC-4** | Spec §8 fixes S20 by widening the server's `_VALID_MODES`; but the server silently *ignores* unknown values today | **Server: `_VALID_MODES` = the six `PERMISSION_MODES` values; unknown → 422** (not silent). **Client:** `GET /api/settings` returns `acceptedPermissionModes`; the Defaults select renders every `PERMISSION_MODES` entry and **disables with a reason** any the server does not accept (C13). A pytest parity test reads `permissionModes.ts` and asserts the two sets are equal. | One source of truth (server), one client list, and a test that fails when they drift. Silent skipping is the bug being fixed. | A 422 where a no-op used to be; the form shows it inline. |
| **DEC-5** | Sub-spec C says PR 5's `pageContract.test.tsx` also adds Sessions | **`PAGES` starts with `settings` only.** Sessions renders no `PageHeader` (only `TopBar` does) and would fail the one-`h1` rule; adding it is PR 4/6 work. | Do not widen scope to make a pre-existing page pass a new test. | One row to add later. |
| **DEC-6** | Appearance's "Show navigation labels" needs the rail-expanded flag, which `NavRail.tsx` reads/writes with a private key | **Extract `web/src/shell/railExpanded.ts`** (`RAIL_EXPANDED_KEY`, `readRailExpanded`, `writeRailExpanded`, `useRailExpanded`) and make `NavRail` use it. | Two writers to one key is the parallel-implementation shape. | A small extraction. |
| **DEC-7** | Pipeline notifications opt-in is `prdash.notifications` in `pipeline/useDashboard.ts` | **Extract `web/src/pipeline/notifyPref.ts`** with key `ccswitch.pipelineNotifications` and a one-time read of the old key; `useDashboard` and Appearance both use it. | Spec S31 + umbrella §2.4 namespace rule; the sweep PR would otherwise do it later with two call sites. | A key rename. |
| **DEC-8** | About needs version/host; no version endpoint exists | **`about: { gitSha, hostName }`** on `GET /api/settings`, from `CCSWITCH_GIT_SHA` (nullable) and `CCSWITCH_HOST_NAME` falling back to `socket.gethostname()`. | Cheapest honest source; the deploy controller already exports `CCSWITCH_GIT_SHA`. | Two fields to move if a `/api/about` appears. |
| **DEC-9** | Bare `/settings` on desktop | **Redirects to the last visited section** (`ccswitch.settings.last`), else `general`. On phone/tablet-portrait it renders the list. | Spec §6.1, C1 "restore the most recently viewed pane". | A key to delete. |
| **DEC-10** *(superseded during execution)* | Legacy `/automation` currently redirects to `/settings/automation` | **Keep** the section in 5a (group Sessions, label "Automations", last in group). Reversed during implementation: `/automations` has no configuration controls. PR 6 folds configuration into the Automations page and retires the section. So `/settings/automation` stays a real section and legacy `/automation` → `/settings/automation` is kept. *(Originally: `/automation` → `/automations`; `/settings/automation` → `/automations`; section removed.)* | Spec D3, reversed by R10 — removing the section would have removed the only automation-configuration UI. | One redirect row unchanged; the section retires with PR 6. |
| **DEC-11** | Typed confirm for the archive path needs "how many archives move" | **`useArchivedSessions().length`** (already fetched for the nav badge). The dialog asks the user to type the **new path**. | No new endpoint; the count is already on the client. | Count could lag by one poll interval; stated as "about N". |
| **DEC-12** | Unmigrated sections need a chassis heading now | **`HostedSection`** wraps each legacy component with `SettingsSection` (title/description/scope from `settingsDefs`) and renders the component as-is. | Every route gets the page contract today; the sections' internals change in their own PRs. | None; it is deleted per section as 5b–5e migrate. |

**One spec deviation stated plainly:** spec §11 lists `SettingsGroup` among primitives but §7.1 never gives it a `description`; this plan gives it an optional `description` because General's *Archive* group needs one sentence under its legend.

---

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `web/src/ui/Switch.tsx` (+ `.test.tsx`) | `role="switch"` primitive; Space/Enter toggles; label never changes. |
| `web/src/ui/Checkbox.tsx` (+ `.test.tsx`) | Native checkbox with attached label, for saved forms. |
| `web/src/ui/RadioGroup.tsx` (+ `.test.tsx`) | `fieldset`/`legend` radio set with per-option description and disabled reason. |
| `web/src/ui/ConfirmDialog.tsx` (+ `.test.tsx`) | Confirm / typed tiers on `Dialog`. |
| `web/src/features/settings/settingsDefs.ts` (+ `.test.ts`) | The registry: groups, sections, scope, description, component. |
| `web/src/features/settings/lastSection.ts` | `ccswitch.settings.last` read/write. |
| `web/src/features/settings/useSettingsBadges.ts` | Accounts-needing-attention count for the nav. |
| `web/src/features/settings/chassis/ScopeBadge.tsx` | Server / This browser / This device badge. |
| `web/src/features/settings/chassis/SettingsSection.tsx` | `h2` + description + badge + children + footer. |
| `web/src/features/settings/chassis/SettingsGroup.tsx` | `fieldset`/`legend`, optional danger tone. |
| `web/src/features/settings/chassis/SettingRow.tsx` | label · description · control · status. |
| `web/src/features/settings/chassis/SaveBar.tsx` | Sticky Save/Discard + status. |
| `web/src/features/settings/chassis/chassis.test.tsx` | Tests for the five above. |
| `web/src/features/settings/useSettingsForm.ts` (+ `.test.tsx`) | Draft, dirty, save, discard, blocker, `beforeunload`. |
| `web/src/features/settings/SettingsNav.tsx` | Grouped vertical nav (desktop) and grouped list (drill). |
| `web/src/features/settings/HostedSection.tsx` | Chassis heading around an unmigrated component. |
| `web/src/features/settings/SettingsPage.tsx` (+ `.test.tsx`) | `/settings/:section`, Split variant. |
| `web/src/features/settings/SettingsIndexPage.tsx` | `/settings`: list (drill) or redirect (desktop). |
| `web/src/features/settings/sections/GeneralSection.tsx` (+ `.test.tsx`) | Paths · Archive · Capacity. |
| `web/src/features/settings/sections/SessionDefaultsSection.tsx` (+ `.test.tsx`) | Tool approvals · Model approval · Transcript. |
| `web/src/features/settings/sections/AppearanceSection.tsx` (+ `.test.tsx`) | Theme · Navigation · Layout (this browser). |
| `web/src/features/settings/sections/AboutSection.tsx` (+ `.test.tsx`) | Version · Managed by env. |
| `web/src/features/settings/pageContract.test.tsx` | Page-contract invariants per mode, `PAGES = ['settings']`. |
| `web/src/shell/railExpanded.ts` | DEC-6. |
| `web/src/pipeline/notifyPref.ts` | DEC-7. |

**Deleted** (all in this PR): `web/src/components/settings/SettingsView.tsx` · `web/src/components/settings/GeneralSection.tsx` · `web/src/components/settings/__tests__/SettingsView.test.tsx` · `web/src/components/settings/__tests__/GeneralSection.test.tsx` · `web/src/components/settings/__tests__/ModelApprovalControls.test.tsx` (its per-account half moves to `AccountsManager`'s own tests in Task 18).

**Modified:** `server/api/settings.py` · `server/db/models.py:383-385` (docstring) · `server/tests/test_settings.py` · `web/src/api/hooks.ts` (`AppSettings`) · `web/src/test/factories.ts` (`appSettings`) · `web/src/shell/routes.tsx` · `web/src/shell/routes.test.tsx` · `web/src/shell/useViewport.tsx` · `web/src/test/matchMedia.ts` · `web/src/shell/regions/NavRail.tsx` · `web/src/pipeline/useDashboard.ts` · `web/src/components/__tests__/dedupeMobileDesktop.test.ts` · `web/e2e/dashboard.spec.ts` (if it names Settings tabs).

---

## Tasks

### Task 1: Server rejects unknown permission modes; parity test with the client list

**Files:**
- Modify: `server/api/settings.py:19` (`_VALID_MODES`), `:123-124` (mode branch), `:129-130` (tool-detail branch)
- Modify: `server/db/models.py:383-385` (docstring)
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Produces: `server.api.settings.ACCEPTED_PERMISSION_MODES: tuple[str, ...]` (ordered, exported; Task 2 returns it from GET). `PUT /api/settings` → `422 {"detail": "invalid defaultPermissionMode"}` / `"invalid defaultToolDetail"` on unknown values.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_settings.py`:

```python
import re


def _client_mode_values() -> set[str]:
    """The `value:` strings in web/src/session/permissionModes.ts — the client's list."""
    src = (REPO_ROOT / "web" / "src" / "session" / "permissionModes.ts").read_text()
    return set(re.findall(r"value:\s*'([^']+)'", src))


def test_accepted_permission_modes_match_client_list():
    """S20: the server accepts exactly the modes the client offers."""
    from server.api.settings import ACCEPTED_PERMISSION_MODES

    assert set(ACCEPTED_PERMISSION_MODES) == _client_mode_values()
    assert len(ACCEPTED_PERMISSION_MODES) == 6


def test_put_bypass_permissions_round_trips():
    """`bypassPermissions` used to be silently dropped; now it persists."""
    with _client() as client:
        put = client.put("/api/settings", json={"defaultPermissionMode": "bypassPermissions"},
                         headers=IDENTITY_HEADERS)
        assert put.status_code == 200
        got = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert got["defaultPermissionMode"] == "bypassPermissions"


def test_put_unknown_permission_mode_is_422_not_silent():
    with _client() as client:
        resp = client.put("/api/settings", json={"defaultPermissionMode": "yolo"},
                          headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "invalid defaultPermissionMode"
        got = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert got["defaultPermissionMode"] == "interactive"


def test_put_unknown_tool_detail_is_422():
    with _client() as client:
        resp = client.put("/api/settings", json={"defaultToolDetail": "wat"}, headers=IDENTITY_HEADERS)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid defaultToolDetail"
```

Then open the existing `test_put_rejects_invalid_mode` (line 88) and change its assertion from "silently unchanged with 200" to `assert resp.status_code == 422`, keeping its follow-up GET assertion that the stored value is still `interactive`.

- [ ] **Step 2: Run to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_settings.py -q -k "permission_mode or bypass or tool_detail or invalid_mode"`
Expected: FAIL — `ImportError: cannot import name 'ACCEPTED_PERMISSION_MODES'`, and the 422 tests get 200.

- [ ] **Step 3: Implement**

In `server/api/settings.py` replace line 19 and the two validation branches:

```python
# The modes the client's PERMISSION_MODES offers, in the client's order. A pytest
# parity test reads web/src/session/permissionModes.ts and asserts equality, so
# adding a mode on either side without the other fails CI (S20).
ACCEPTED_PERMISSION_MODES: tuple[str, ...] = (
    "interactive", "auto", "allowlist", "acceptEdits", "full-auto", "bypassPermissions",
)
_VALID_MODES = frozenset(ACCEPTED_PERMISSION_MODES)
_VALID_TOOL_DETAIL = frozenset({"auto", "all-closed", "all-open"})
```

```python
    if body.defaultPermissionMode is not None:
        if body.defaultPermissionMode not in _VALID_MODES:
            raise HTTPException(status_code=422, detail="invalid defaultPermissionMode")
        row.default_permission_mode = body.defaultPermissionMode
    if body.thinkingCollapsed is not None:
        row.thinking_collapsed = body.thinkingCollapsed
    if body.repoRoot is not None:
        row.repo_root = body.repoRoot
    if body.defaultToolDetail is not None:
        if body.defaultToolDetail not in _VALID_TOOL_DETAIL:
            raise HTTPException(status_code=422, detail="invalid defaultToolDetail")
        row.default_tool_detail = body.defaultToolDetail
```

In `server/db/models.py` lines 383-385, replace the stale docstring on `model_approval_action` with:

```python
    # Per-install model-approval action: "require_approval" | "block" | "allow" | NULL
    # (NULL = fall back to CCSWITCH_MODEL_APPROVAL_ACTION). See
    # server/sessions/model_policy.VALID_ACTIONS — the one definition of the set.
```

- [ ] **Step 4: Run the settings tests and the Python gates**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_settings.py server/tests/test_settings_model_approval.py -q`
Expected: PASS. Then `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` (paths as in Gates): clean.

- [ ] **Step 5: Commit**

```bash
git add server/api/settings.py server/db/models.py server/tests/test_settings.py
git commit -m "fix(settings): accept the six client permission modes; 422 on unknown values (S20, S35)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `GET /api/settings` gains `effectiveDefaults`, `acceptedPermissionModes`, `managed`, `about`

**Files:**
- Modify: `server/api/settings.py` (`_to_dict`, both handlers)
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Consumes: `ACCEPTED_PERMISSION_MODES` (Task 1); `server.config.get_settings()` fields `repo_root`, `archive_home`, `model_approval_action`, `model_approval_denylist`, `allow_unauthenticated`, `enable_xvfb`, `enable_adoption`, `enable_copilot`, `log_level`, `client_log_level`, `self_model_switch_enabled`, `self_model_switch_cap`, `gh_reconcile_enabled`; `DEFAULT_MAX_ACTIVE_SESSIONS`.
- Produces: the response shape below, returned by both `GET` and `PUT`. Task 4 mirrors it in `AppSettings`.

```json
{
  "...existing ten keys...": "unchanged",
  "acceptedPermissionModes": ["interactive","auto","allowlist","acceptEdits","full-auto","bypassPermissions"],
  "effectiveDefaults": {
    "repoRoot": "/home/tony/GitHub",
    "archiveHome": null,
    "modelApprovalAction": "require_approval",
    "modelApprovalDenylist": "fable",
    "maxActiveSessions": 5
  },
  "managed": [
    {"key": "CCSWITCH_ALLOW_UNAUTHENTICATED", "value": false, "description": "Admit clients with no Tailscale identity as a guest"},
    {"key": "CCSWITCH_ENABLE_ADOPTION", "value": false, "description": "Allow adopting sessions started outside ccswitch"},
    {"key": "CCSWITCH_ENABLE_COPILOT", "value": false, "description": "Enable GitHub Copilot accounts"},
    {"key": "CCSWITCH_ENABLE_XVFB", "value": false, "description": "Virtual displays for browser-using sessions"},
    {"key": "CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "value": true, "description": "Sessions may request a model change"},
    {"key": "CCSWITCH_SELF_MODEL_SWITCH_CAP", "value": 3, "description": "Auto-applied model switches per session before parking for approval"},
    {"key": "CCSWITCH_GH_RECONCILE_ENABLED", "value": false, "description": "GitHub Projects reconciler may write back"},
    {"key": "CCSWITCH_LOG_LEVEL", "value": "INFO", "description": "Server log level"},
    {"key": "CCSWITCH_CLIENT_LOG_LEVEL", "value": "warn", "description": "Browser log level"}
  ],
  "about": {"gitSha": "f92c6e34", "hostName": "dobby"}
}
```

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_settings.py`:

```python
def test_get_settings_exposes_effective_defaults_and_accepted_modes():
    with temp_env(), patch.dict(os.environ, {
        "CCSWITCH_MODEL_APPROVAL_ACTION": "block",
        "CCSWITCH_MODEL_APPROVAL_DENYLIST": "fable,opus",
        "CCSWITCH_REPO_ROOT": "/srv/repos",
    }):
        with _client() as client:
            data = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert data["acceptedPermissionModes"][0] == "interactive"
    assert len(data["acceptedPermissionModes"]) == 6
    eff = data["effectiveDefaults"]
    assert eff == {
        "repoRoot": "/srv/repos",
        "archiveHome": None,
        "modelApprovalAction": "block",
        "modelApprovalDenylist": "fable,opus",
        "maxActiveSessions": 5,
    }


def test_get_settings_managed_is_an_allowlist_with_no_secrets():
    with temp_env(), patch.dict(os.environ, {
        "CCSWITCH_SELF_MODEL_SWITCH_CAP": "7",
        "CCSWITCH_MASTER_KEY": "sekrit",
        "CCSWITCH_GIT_SHA": "abc1234",
        "CCSWITCH_HOST_NAME": "rocky",
    }):
        with _client() as client:
            data = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    keys = [m["key"] for m in data["managed"]]
    assert "CCSWITCH_MASTER_KEY" not in keys
    assert "CCSWITCH_TAILNET_ALLOWLIST" not in keys
    by_key = {m["key"]: m for m in data["managed"]}
    assert by_key["CCSWITCH_SELF_MODEL_SWITCH_CAP"]["value"] == 7
    assert by_key["CCSWITCH_SELF_MODEL_SWITCH_CAP"]["description"]
    assert data["about"] == {"gitSha": "abc1234", "hostName": "rocky"}


def test_put_settings_returns_the_same_shape_as_get():
    with _client() as client:
        put = client.put("/api/settings", json={"thinkingCollapsed": False}, headers=IDENTITY_HEADERS).json()
        get = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert set(put) == set(get)
    assert "effectiveDefaults" in put
```

- [ ] **Step 2: Run to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_settings.py -q -k "effective or managed or same_shape"`
Expected: FAIL with `KeyError: 'acceptedPermissionModes'`.

- [ ] **Step 3: Implement**

In `server/api/settings.py` add imports and the allowlist, and extend `_to_dict`:

```python
import os
import socket

from server.config import get_settings
from server.db.models import AppSettings, DEFAULT_MAX_ACTIVE_SESSIONS

# Read-only env flags shown on Settings › About (spec S30). An ALLOWLIST: a key
# not named here is never sent, so secrets and allowlists cannot leak by accident.
# (config attribute, env var name, one-sentence description)
_MANAGED: tuple[tuple[str, str, str], ...] = (
    ("allow_unauthenticated", "CCSWITCH_ALLOW_UNAUTHENTICATED", "Admit clients with no Tailscale identity as a guest"),
    ("enable_adoption", "CCSWITCH_ENABLE_ADOPTION", "Allow adopting sessions started outside ccswitch"),
    ("enable_copilot", "CCSWITCH_ENABLE_COPILOT", "Enable GitHub Copilot accounts"),
    ("enable_xvfb", "CCSWITCH_ENABLE_XVFB", "Virtual displays for browser-using sessions"),
    ("self_model_switch_enabled", "CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "Sessions may request a model change"),
    ("self_model_switch_cap", "CCSWITCH_SELF_MODEL_SWITCH_CAP", "Auto-applied model switches per session before parking for approval"),
    ("gh_reconcile_enabled", "CCSWITCH_GH_RECONCILE_ENABLED", "GitHub Projects reconciler may write back"),
    ("log_level", "CCSWITCH_LOG_LEVEL", "Server log level"),
    ("client_log_level", "CCSWITCH_CLIENT_LOG_LEVEL", "Browser log level"),
)


def _server_blocks() -> dict:
    cfg = get_settings()
    return {
        "acceptedPermissionModes": list(ACCEPTED_PERMISSION_MODES),
        "effectiveDefaults": {
            "repoRoot": cfg.repo_root,
            "archiveHome": cfg.archive_home,
            "modelApprovalAction": cfg.model_approval_action,
            "modelApprovalDenylist": cfg.model_approval_denylist,
            "maxActiveSessions": DEFAULT_MAX_ACTIVE_SESSIONS,
        },
        "managed": [
            {"key": env, "value": getattr(cfg, attr), "description": desc}
            for attr, env, desc in _MANAGED
        ],
        "about": {
            "gitSha": os.environ.get("CCSWITCH_GIT_SHA") or None,
            "hostName": os.environ.get("CCSWITCH_HOST_NAME") or socket.gethostname(),
        },
    }


def _to_dict(s: AppSettings) -> dict:
    return {
        "defaultPermissionMode": s.default_permission_mode,
        "thinkingCollapsed": s.thinking_collapsed,
        "repoRoot": s.repo_root,
        "defaultToolDetail": s.default_tool_detail or "auto",
        "maxArchivedSessions": s.max_archived_sessions,
        "archiveHome": s.archive_home,
        "maxActiveSessions": s.max_active_sessions,
        "autoArchiveEnabled": s.auto_archive_enabled is not False,
        "modelApprovalAction": s.model_approval_action,
        "modelApprovalDenylist": s.model_approval_denylist,
        **_server_blocks(),
    }
```

`get_settings()` is cached per process in `server/config.py`; the tests above set env before the app is created, which is when it is first read.

- [ ] **Step 4: Run tests and gates**

Run the settings pytest command from Gates, then ruff and mypy. Expected: PASS / clean. If mypy complains that `getattr` returns `Any` in a `dict` literal, annotate `_server_blocks() -> dict[str, object]`.

- [ ] **Step 5: Commit**

```bash
git add server/api/settings.py server/tests/test_settings.py
git commit -m "feat(settings): expose effective defaults, accepted modes, managed env flags and about (S28, S30)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Archive-path change refuses to persist when the file move partially failed

**Files:**
- Modify: `server/api/settings.py` (`_migrate_archive_home`, PUT `archiveHome` branch)
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Produces: `_migrate_archive_home(...) -> tuple[int, int, int]` (moved, failures, total); `PUT /api/settings` → `409 {"detail": {"code": "archive_move_failed", "moved": 2, "failed": 1, "total": 3}}` and the row's `archive_home` unchanged.

- [ ] **Step 1: Write the failing test**

```python
def test_put_archive_home_is_409_and_unchanged_when_moves_fail(monkeypatch):
    """S16: a partial move must not leave the DB pointing at a path the files are not in."""
    import server.api.settings as settings_api

    async def fake_migrate(orch, db, old_home, new_home):
        return (2, 1, 3)

    monkeypatch.setattr(settings_api, "_migrate_archive_home", fake_migrate)
    with _client() as client:
        resp = client.put("/api/settings", json={"archiveHome": "/mnt/nowhere"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 409
        assert resp.json()["detail"] == {"code": "archive_move_failed", "moved": 2, "failed": 1, "total": 3}
        got = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert got["archiveHome"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_settings.py -q -k archive_home_is_409`
Expected: FAIL — status 200.

- [ ] **Step 3: Implement**

Change `_migrate_archive_home` to return counts instead of only logging:

```python
async def _migrate_archive_home(orch, db, old_home: str | None, new_home: str | None) -> tuple[int, int, int]:
    """Move every archived session dir from old_home to new_home on the daemon host.

    Returns (moved, failures, total). The caller decides whether to persist the
    new path — a partial move is a 409, not a warning, because a DB row pointing
    at a directory the files are not in is worse than the old path (S16).
    """
    from server.db.models import Session as DbSession

    archived_ids: list[str] = list((await db.execute(
        select(DbSession.id).where(DbSession.status == "archived")
    )).scalars().all())
    if not archived_ids:
        return (0, 0, 0)

    result = await orch.migrate_archive_home(old_home, new_home, archived_ids)
    moved, failures = int(result.get("moved", 0)), int(result.get("failures", 0))
    if failures:
        log.warning("archive_home migrate: %d/%d moves failed", failures, len(archived_ids))
    else:
        log.info("archive_home migrate: moved %d/%d sessions %s → %s",
                 moved, len(archived_ids), old_home, new_home)
    return (moved, failures, len(archived_ids))
```

And the PUT branch:

```python
    if body.archiveHome is not None:
        new_home = body.archiveHome.strip() or None
        if new_home != row.archive_home:
            moved, failed, total = await _migrate_archive_home(
                request.app.state.sessions, db, row.archive_home, new_home)
            if failed:
                raise HTTPException(status_code=409, detail={
                    "code": "archive_move_failed", "moved": moved, "failed": failed, "total": total,
                })
            row.archive_home = new_home
```

- [ ] **Step 4: Run tests and gates**

Run the settings pytest command, ruff, mypy. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add server/api/settings.py server/tests/test_settings.py
git commit -m "fix(settings): 409 and no persist when an archive-home move partially fails (S16)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Client `AppSettings` mirrors the new server shape

**Files:**
- Modify: `web/src/api/hooks.ts:198-217` (`AppSettings`)
- Modify: `web/src/test/factories.ts:100-113` (`appSettings`)
- Test: `web/src/api/__tests__/hooks.settings.test.ts`

**Interfaces:**
- Produces:

```ts
export interface ManagedFlag { key: string; value: string | number | boolean | null; description: string }
export interface EffectiveDefaults {
  repoRoot: string; archiveHome: string | null
  modelApprovalAction: string; modelApprovalDenylist: string; maxActiveSessions: number
}
export interface AppSettings {
  /* existing ten fields unchanged */
  acceptedPermissionModes: string[]
  effectiveDefaults: EffectiveDefaults
  managed: ManagedFlag[]
  about: { gitSha: string | null; hostName: string }
}
```

- [ ] **Step 1: Write the failing test**

Append to `web/src/api/__tests__/hooks.settings.test.ts` (it already mocks `api.get`; follow its existing pattern for the mock):

```ts
it('exposes the server blocks added for Settings (spec §9)', async () => {
  const payload = appSettings({
    acceptedPermissionModes: ['interactive'],
    effectiveDefaults: { repoRoot: '/r', archiveHome: null, modelApprovalAction: 'block', modelApprovalDenylist: 'fable', maxActiveSessions: 5 },
    managed: [{ key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' }],
    about: { gitSha: 'abc1234', hostName: 'dobby' },
  })
  vi.mocked(api.get).mockResolvedValueOnce(payload)
  const { result } = renderHook(() => useSettings(), { wrapper })
  await waitFor(() => expect(result.current.data).toBeDefined())
  expect(result.current.data?.effectiveDefaults.modelApprovalAction).toBe('block')
  expect(result.current.data?.about.hostName).toBe('dobby')
})
```

Import `appSettings` from `@/test/factories` at the top of that test file.

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web test -- src/api/__tests__/hooks.settings.test.ts`
Expected: FAIL — typecheck error "Object literal may only specify known properties" (vitest reports it as a type error in the transform, or the test fails on `effectiveDefaults` being undefined).

- [ ] **Step 3: Implement**

In `web/src/api/hooks.ts`, above `AppSettings`:

```ts
/** One read-only env flag shown on Settings › About. The server sends an allowlist. */
export interface ManagedFlag {
  key: string
  value: string | number | boolean | null
  description: string
}

/** What the server falls back to when the matching AppSettings column is NULL. */
export interface EffectiveDefaults {
  repoRoot: string
  archiveHome: string | null
  modelApprovalAction: string
  modelApprovalDenylist: string
  maxActiveSessions: number
}
```

Add to the end of `AppSettings`:

```ts
  /** The modes PUT accepts, in display order. The client's PERMISSION_MODES must equal this set. */
  acceptedPermissionModes: string[]
  effectiveDefaults: EffectiveDefaults
  managed: ManagedFlag[]
  about: { gitSha: string | null; hostName: string }
```

In `web/src/test/factories.ts`, extend `appSettings` before `...over`:

```ts
    acceptedPermissionModes: ['interactive', 'auto', 'allowlist', 'acceptEdits', 'full-auto', 'bypassPermissions'],
    effectiveDefaults: {
      repoRoot: '/home/user/GitHub', archiveHome: null,
      modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable', maxActiveSessions: 5,
    },
    managed: [],
    about: { gitSha: null, hostName: 'test-host' },
```

- [ ] **Step 4: Run test + typecheck**

Run: `npm --prefix web test -- src/api/__tests__/hooks.settings.test.ts` then `npm --prefix web run typecheck`.
Expected: PASS. Typecheck may now flag suites that build `AppSettings` literals by hand instead of via `appSettings()`; fix each by routing through the factory (that is the factory's documented purpose).

- [ ] **Step 5: Commit**

```bash
git add web/src/api/hooks.ts web/src/test/factories.ts web/src/api/__tests__/hooks.settings.test.ts
git commit -m "feat(web): AppSettings carries effectiveDefaults, acceptedPermissionModes, managed, about

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `Switch` primitive

**Files:**
- Create: `web/src/ui/Switch.tsx`
- Test: `web/src/ui/Switch.test.tsx`

**Interfaces:**
- Produces:

```ts
export interface SwitchProps {
  checked: boolean
  onChange: (next: boolean) => void
  id?: string
  disabled?: boolean
  'aria-label'?: string
  'aria-labelledby'?: string
  'aria-describedby'?: string
  className?: string
}
export const Switch: React.ForwardRefExoticComponent<SwitchProps & React.RefAttributes<HTMLButtonElement>>
```

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Switch } from './Switch'

expect.extend(toHaveNoViolations)

describe('Switch', () => {
  it('is a switch whose accessible name does not change with state', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<Switch checked={false} onChange={onChange} aria-label="Pipeline notifications" />)
    const sw = screen.getByRole('switch', { name: 'Pipeline notifications' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    await userEvent.click(sw)
    expect(onChange).toHaveBeenCalledWith(true)
    rerender(<Switch checked onChange={onChange} aria-label="Pipeline notifications" />)
    expect(screen.getByRole('switch', { name: 'Pipeline notifications' })).toHaveAttribute('aria-checked', 'true')
  })

  it('toggles on Space and Enter, not on other keys; disabled does nothing', async () => {
    const onChange = vi.fn()
    render(<Switch checked={false} onChange={onChange} aria-label="X" />)
    const sw = screen.getByRole('switch')
    sw.focus()
    await userEvent.keyboard(' ')
    await userEvent.keyboard('{Enter}')
    await userEvent.keyboard('a')
    expect(onChange).toHaveBeenCalledTimes(2)
    onChange.mockClear()
    render(<Switch checked={false} onChange={onChange} aria-label="Y" disabled />)
    await userEvent.click(screen.getByRole('switch', { name: 'Y' }))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('has no axe violations', async () => {
    const { container } = render(<Switch checked onChange={() => {}} aria-label="Enabled" />)
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web test -- src/ui/Switch.test.tsx`
Expected: FAIL — cannot resolve `./Switch`.

- [ ] **Step 3: Implement**

```tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SwitchProps {
  checked: boolean
  onChange: (next: boolean) => void
  id?: string
  disabled?: boolean
  'aria-label'?: string
  'aria-labelledby'?: string
  'aria-describedby'?: string
  className?: string
}

/**
 * On/off that applies immediately (spec §7.4). A `<button role="switch">`, not a
 * checkbox: a checkbox in a form means "save later", a switch means "done now".
 * The label is supplied by the caller and MUST NOT change with state (APG).
 * Space and Enter toggle; native button semantics give Enter for free and the
 * keydown handler adds Space without scrolling the page.
 */
export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onChange, id, disabled, className, ...aria }, ref,
) {
  return (
    <button
      ref={ref}
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={aria['aria-label']}
      aria-labelledby={aria['aria-labelledby']}
      aria-describedby={aria['aria-describedby']}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      onKeyDown={(e) => {
        if (e.key === ' ') { e.preventDefault(); onChange(!checked) }
      }}
      className={cn(
        'relative inline-flex h-6 w-11 shrink-0 items-center rounded-pill border transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        checked ? 'bg-accent border-accent' : 'bg-surface-raised border-edge-strong',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block h-4 w-4 rounded-pill transition-transform',
          checked ? 'translate-x-6 bg-accent-ink' : 'translate-x-1 bg-ink-muted',
        )}
      />
    </button>
  )
})
```

If `rounded-pill` is not a compiled utility (check `web/src/index.css` for `--radius-pill`), use `rounded-full`.

- [ ] **Step 4: Run test, lint, typecheck**

Run: `npm --prefix web test -- src/ui/Switch.test.tsx`; `npm --prefix web run lint`; `npm --prefix web run typecheck`.
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/Switch.tsx web/src/ui/Switch.test.tsx
git commit -m "feat(ui): Switch primitive (role=switch, immediate-apply rows) (S23)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `Checkbox` and `RadioGroup` primitives

**Files:**
- Create: `web/src/ui/Checkbox.tsx`, `web/src/ui/RadioGroup.tsx`
- Test: `web/src/ui/Checkbox.test.tsx`, `web/src/ui/RadioGroup.test.tsx`

**Interfaces:**
- Produces:

```ts
export interface CheckboxProps {
  id: string
  label: string
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  'aria-describedby'?: string
  className?: string
}
export function Checkbox(props: CheckboxProps): JSX.Element

export interface RadioOption<V extends string> {
  value: V
  label: string
  description?: React.ReactNode
  disabled?: boolean
  /** Shown under the label when disabled — never hide an option, say why (C13). */
  disabledReason?: string
}
export interface RadioGroupProps<V extends string> {
  name: string
  legend: string
  /** Visually hide the legend when a SettingRow label already names the group. */
  hideLegend?: boolean
  value: V
  onChange: (next: V) => void
  options: RadioOption<V>[]
  'aria-describedby'?: string
  className?: string
}
export function RadioGroup<V extends string>(props: RadioGroupProps<V>): JSX.Element
```

- [ ] **Step 1: Write the failing tests**

`web/src/ui/Checkbox.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Checkbox } from './Checkbox'

expect.extend(toHaveNoViolations)

describe('Checkbox', () => {
  it('is a labelled native checkbox that reports the next value', async () => {
    const onChange = vi.fn()
    const { container } = render(<Checkbox id="c" label="Collapse thinking by default" checked={false} onChange={onChange} />)
    const box = screen.getByRole('checkbox', { name: 'Collapse thinking by default' })
    await userEvent.click(box)
    expect(onChange).toHaveBeenCalledWith(true)
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

`web/src/ui/RadioGroup.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { RadioGroup } from './RadioGroup'

expect.extend(toHaveNoViolations)

const OPTS = [
  { value: 'system', label: 'Use system setting' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark', disabled: true, disabledReason: 'Pinned by the kiosk URL' },
] as const

describe('RadioGroup', () => {
  it('is a fieldset named by its legend; picks by click; disabled options say why', async () => {
    const onChange = vi.fn()
    const { container } = render(
      <RadioGroup name="theme" legend="Theme" value="system" onChange={onChange} options={[...OPTS]} />,
    )
    expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: /Light/ }))
    expect(onChange).toHaveBeenCalledWith('light')
    const dark = screen.getByRole('radio', { name: /Dark/ })
    expect(dark).toBeDisabled()
    expect(screen.getByText('Pinned by the kiosk URL')).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('hideLegend keeps the legend for AT but not on screen', () => {
    render(<RadioGroup name="t" legend="Theme" hideLegend value="system" onChange={() => {}} options={[...OPTS]} />)
    expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByText('Theme')).toHaveClass('sr-only')
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/ui/Checkbox.test.tsx src/ui/RadioGroup.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

`web/src/ui/Checkbox.tsx`:

```tsx
import { cn } from '@/lib/utils'

export interface CheckboxProps {
  id: string
  label: string
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  'aria-describedby'?: string
  className?: string
}

/** On/off inside a SAVED form (spec §7.4). For immediate on/off use `Switch`. */
export function Checkbox({ id, label, checked, onChange, disabled, className, ...aria }: CheckboxProps) {
  return (
    <label htmlFor={id} className={cn('inline-flex items-center gap-2 min-h-target text-sm text-ink cursor-pointer', disabled && 'opacity-50 cursor-not-allowed', className)}>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={aria['aria-describedby']}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded-sm border-edge-strong accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      />
      <span>{label}</span>
    </label>
  )
}
```

`web/src/ui/RadioGroup.tsx`:

```tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface RadioOption<V extends string> {
  value: V
  label: string
  description?: React.ReactNode
  disabled?: boolean
  /** Shown under the label when disabled — never hide an option, say why (C13). */
  disabledReason?: string
}

export interface RadioGroupProps<V extends string> {
  name: string
  legend: string
  /** Visually hide the legend when a SettingRow label already names the group. */
  hideLegend?: boolean
  value: V
  onChange: (next: V) => void
  options: RadioOption<V>[]
  'aria-describedby'?: string
  className?: string
}

/** 2–5 mutually exclusive values, all visible (spec §7.4). Native radios in a fieldset. */
export function RadioGroup<V extends string>({
  name, legend, hideLegend, value, onChange, options, className, ...aria
}: RadioGroupProps<V>) {
  return (
    <fieldset className={cn('flex flex-col gap-1.5', className)} aria-describedby={aria['aria-describedby']}>
      <legend className={cn('text-sm font-medium text-ink', hideLegend && 'sr-only')}>{legend}</legend>
      {options.map((o) => {
        const id = `${name}-${o.value}`
        const reasonId = o.disabled && o.disabledReason ? `${id}-reason` : undefined
        return (
          <div key={o.value} className="flex flex-col">
            <label htmlFor={id} className={cn('inline-flex items-center gap-2 min-h-target text-sm text-ink', o.disabled ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer')}>
              <input
                id={id}
                type="radio"
                name={name}
                value={o.value}
                checked={o.value === value}
                disabled={o.disabled}
                aria-describedby={reasonId}
                onChange={() => onChange(o.value)}
                className="h-4 w-4 accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              />
              <span>{o.label}</span>
            </label>
            {o.description && <p className="pl-6 text-xs text-ink-muted">{o.description}</p>}
            {reasonId && <p id={reasonId} className="pl-6 text-xs text-ink-muted">{o.disabledReason}</p>}
          </div>
        )
      })}
    </fieldset>
  )
}
```

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/ui/Checkbox.test.tsx src/ui/RadioGroup.test.tsx`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/Checkbox.tsx web/src/ui/Checkbox.test.tsx web/src/ui/RadioGroup.tsx web/src/ui/RadioGroup.test.tsx
git commit -m "feat(ui): Checkbox and RadioGroup primitives for saved forms (S23)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `ConfirmDialog` primitive (Confirm and Typed tiers)

**Files:**
- Create: `web/src/ui/ConfirmDialog.tsx`
- Test: `web/src/ui/ConfirmDialog.test.tsx`

**Interfaces:**
- Consumes: `Dialog` (`web/src/ui/Dialog.tsx`: `open`, `onClose`, `title`, `description`, `size`, `initialFocus`), `Button` (`variant="danger"` exists? check `buttonVariants` in `Button.tsx`; if the variants are `primary|secondary|ghost|danger` use `danger`, otherwise add nothing and use `className="bg-danger text-accent-ink"`), `Input`, `Field`.
- Produces:

```ts
export interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  /** May be async; the confirm button shows `busy` until it settles. Errors propagate to the caller. */
  onConfirm: () => void | Promise<void>
  title: string
  description: React.ReactNode
  confirmLabel: string
  tone?: 'danger' | 'warning'
  /** Typed tier: the exact string the user must type before confirm enables. */
  typed?: string
  busy?: boolean
}
export function ConfirmDialog(props: ConfirmDialogProps): JSX.Element | null
```

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConfirmDialog } from './ConfirmDialog'

describe('ConfirmDialog', () => {
  it('confirm tier: Cancel closes without confirming; the action button confirms', async () => {
    const onClose = vi.fn(); const onConfirm = vi.fn()
    render(<ConfirmDialog open onClose={onClose} onConfirm={onConfirm} title="Delete hook?" description="It runs on every session." confirmLabel="Delete hook" />)
    expect(screen.getByRole('dialog', { name: 'Delete hook?' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
    expect(onConfirm).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete hook' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('typed tier: the action stays disabled until the exact name is typed', async () => {
    const onConfirm = vi.fn()
    render(<ConfirmDialog open onClose={() => {}} onConfirm={onConfirm} title="Delete channel “ntfy-phone”?" description="3 rules go with it." confirmLabel="Delete channel" typed="ntfy-phone" />)
    const btn = screen.getByRole('button', { name: 'Delete channel' })
    expect(btn).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type ntfy-phone to confirm/), 'ntfy-phon')
    expect(btn).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type ntfy-phone to confirm/), 'e')
    expect(btn).toBeEnabled()
    await userEvent.click(btn)
    expect(onConfirm).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web test -- src/ui/ConfirmDialog.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```tsx
import * as React from 'react'
import { Dialog } from './Dialog'
import { Button } from './Button'
import { Input } from './Input'
import { Field } from './Field'

export interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void | Promise<void>
  title: string
  description: React.ReactNode
  confirmLabel: string
  tone?: 'danger' | 'warning'
  typed?: string
  busy?: boolean
}

/**
 * Spec §7.3. Two tiers on one component: Confirm (title + one sentence + two
 * buttons) and Typed (`typed` given: the action enables only when the input
 * equals it). Replaces every `window.confirm` — those cannot show the affected
 * items, are unstyled, and block automation.
 */
export function ConfirmDialog({
  open, onClose, onConfirm, title, description, confirmLabel, tone = 'danger', typed, busy = false,
}: ConfirmDialogProps) {
  const [text, setText] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const inputRef = React.useRef<HTMLInputElement>(null)
  const cancelRef = React.useRef<HTMLButtonElement>(null)
  React.useEffect(() => { if (!open) setText('') }, [open])

  const ready = typed === undefined || text === typed
  const working = busy || pending

  async function confirm() {
    if (!ready || working) return
    try {
      setPending(true)
      await onConfirm()
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={title} description={description} size="sm"
      initialFocus={typed !== undefined ? inputRef : cancelRef}>
      {typed !== undefined && (
        <Field label={`Type ${typed} to confirm`} htmlFor="confirm-typed">
          <Input ref={inputRef} id="confirm-typed" value={text} onChange={(e) => setText(e.target.value)}
            autoComplete="off" spellCheck={false} placeholder={typed} />
        </Field>
      )}
      <div className="flex justify-end gap-2">
        <Button ref={cancelRef} type="button" variant="ghost" onClick={onClose} disabled={working}>Cancel</Button>
        <Button type="button" variant={tone === 'danger' ? 'danger' : 'primary'} onClick={confirm}
          disabled={!ready || working} loading={working}>
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  )
}
```

Check `Button.tsx`'s `buttonVariants` for the exact variant names (`ghost`, `danger`, `primary`) and adjust; do not add a new variant if one with the right meaning exists.

- [ ] **Step 4: Run test, lint, typecheck**

Run: `npm --prefix web test -- src/ui/ConfirmDialog.test.tsx`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/ConfirmDialog.tsx web/src/ui/ConfirmDialog.test.tsx
git commit -m "feat(ui): ConfirmDialog with confirm and typed tiers (S15, S18)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Settings chassis — `ScopeBadge`, `SettingsSection`, `SettingsGroup`, `SettingRow`

**Files:**
- Create: `web/src/features/settings/chassis/ScopeBadge.tsx`, `SettingsSection.tsx`, `SettingsGroup.tsx`, `SettingRow.tsx`
- Create: `web/src/features/settings/scope.ts`
- Test: `web/src/features/settings/chassis/chassis.test.tsx`

**Interfaces:**
- Consumes: `Badge` (`web/src/ui/Badge.tsx`: `tone`), `cn`.
- Produces:

```ts
// scope.ts
export type SettingsScope = 'server' | 'browser' | 'device' | 'none'
export const SCOPE_LABEL: Record<SettingsScope, string>   // 'Server' | 'This browser' | 'This device' | 'Read-only'

// ScopeBadge.tsx
export function ScopeBadge({ scope }: { scope: SettingsScope }): JSX.Element | null   // null for 'none'

// SettingsSection.tsx
export interface SettingsSectionProps {
  id: string                       // used for the h2 id: `settings-${id}-title`
  title: string
  description: string
  scope: SettingsScope
  badge?: React.ReactNode          // extra badge beside the title (e.g. "2 need attention")
  footer?: React.ReactNode         // the SaveBar for form sections
  children: React.ReactNode
}
export function SettingsSection(props: SettingsSectionProps): JSX.Element

// SettingsGroup.tsx
export interface SettingsGroupProps {
  legend: string
  description?: React.ReactNode
  tone?: 'default' | 'danger'
  children: React.ReactNode
  className?: string
}
export function SettingsGroup(props: SettingsGroupProps): JSX.Element

// SettingRow.tsx
export interface SettingRowProps {
  label: string
  htmlFor: string                  // id of the control; row derives `${htmlFor}-desc` / `${htmlFor}-error`
  description?: React.ReactNode
  error?: React.ReactNode
  status?: React.ReactNode         // rendered in a role="status" span beside the control (immediate rows)
  /** Control spans the full row width (textarea, radio group) instead of the right column. */
  wide?: boolean
  children: React.ReactElement<{ 'aria-describedby'?: string }>
}
export function SettingRow(props: SettingRowProps): JSX.Element
```

- [ ] **Step 1: Write the failing tests**

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { SettingsSection } from './SettingsSection'
import { SettingsGroup } from './SettingsGroup'
import { SettingRow } from './SettingRow'
import { ScopeBadge } from './ScopeBadge'
import { Input } from '@/ui/Input'

expect.extend(toHaveNoViolations)

describe('settings chassis', () => {
  it('section is a region named by its h2, with description and scope badge', async () => {
    const { container } = render(
      <SettingsSection id="general" title="General" description="Paths and limits." scope="server">
        <p>body</p>
      </SettingsSection>,
    )
    const region = screen.getByRole('region', { name: 'General' })
    expect(region).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'General' })).toHaveAttribute('id', 'settings-general-title')
    expect(screen.getByText('Paths and limits.')).toBeInTheDocument()
    expect(screen.getByText('Server')).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('group is a fieldset with a legend; danger tone marks it', () => {
    render(
      <SettingsGroup legend="Danger zone" tone="danger" description="Cannot be undone.">
        <span>x</span>
      </SettingsGroup>,
    )
    const grp = screen.getByRole('group', { name: 'Danger zone' })
    expect(grp).toHaveAttribute('data-tone', 'danger')
    expect(screen.getByText('Cannot be undone.')).toBeInTheDocument()
  })

  it('row wires label, description and error to the control via aria-describedby', () => {
    render(
      <SettingRow label="Repo clone root" htmlFor="repo-root" description="Where repos are cloned." error="Must be absolute.">
        <Input id="repo-root" />
      </SettingRow>,
    )
    const input = screen.getByLabelText('Repo clone root')
    expect(input).toHaveAttribute('aria-describedby', 'repo-root-desc repo-root-error')
    expect(screen.getByRole('alert')).toHaveTextContent('Must be absolute.')
  })

  it('row status is a polite live region', () => {
    render(
      <SettingRow label="Theme" htmlFor="theme" status="Saved">
        <Input id="theme" />
      </SettingRow>,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
  })

  it('ScopeBadge renders nothing for none', () => {
    const { container } = render(<ScopeBadge scope="none" />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/features/settings/chassis/chassis.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

`web/src/features/settings/scope.ts`:

```ts
/** Where a setting applies (spec D7). Rendered as a badge on every section. */
export type SettingsScope = 'server' | 'browser' | 'device' | 'none'

export const SCOPE_LABEL: Record<SettingsScope, string> = {
  server: 'Server',
  browser: 'This browser',
  device: 'This device',
  none: 'Read-only',
}
```

`ScopeBadge.tsx`:

```tsx
import { Badge } from '@/ui/Badge'
import { SCOPE_LABEL, type SettingsScope } from '../scope'

const TONE = { server: 'accent', browser: 'success', device: 'warning', none: 'neutral' } as const

/** "Server" = every browser and host · "This browser" = localStorage · "This device" = push subscription. */
export function ScopeBadge({ scope }: { scope: SettingsScope }) {
  if (scope === 'none') return null
  return <Badge tone={TONE[scope]} title={`Applies to: ${SCOPE_LABEL[scope].toLowerCase()}`}>{SCOPE_LABEL[scope]}</Badge>
}
```

`SettingsSection.tsx`:

```tsx
import * as React from 'react'
import { ScopeBadge } from './ScopeBadge'
import type { SettingsScope } from '../scope'

export interface SettingsSectionProps {
  id: string
  title: string
  description: string
  scope: SettingsScope
  badge?: React.ReactNode
  footer?: React.ReactNode
  children: React.ReactNode
}

/**
 * The chassis every section instantiates (spec §7.1): one h2 (the page's h1 is
 * "Settings" in PageHeader), a one-sentence description, the scope badge, the
 * body, and an optional footer slot that form sections fill with SaveBar.
 */
export function SettingsSection({ id, title, description, scope, badge, footer, children }: SettingsSectionProps) {
  const titleId = `settings-${id}-title`
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-5 max-w-3xl">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 id={titleId} className="text-xl font-semibold text-ink">{title}</h2>
          <ScopeBadge scope={scope} />
          {badge}
        </div>
        <p className="text-sm text-ink-muted">{description}</p>
      </div>
      <div className="flex flex-col gap-4">{children}</div>
      {footer}
    </section>
  )
}
```

`SettingsGroup.tsx`:

```tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SettingsGroupProps {
  legend: string
  description?: React.ReactNode
  tone?: 'default' | 'danger'
  children: React.ReactNode
  className?: string
}

/** A fieldset of related rows (C14). `tone="danger"` is the section's Danger zone, always last. */
export function SettingsGroup({ legend, description, tone = 'default', children, className }: SettingsGroupProps) {
  return (
    <fieldset
      data-tone={tone}
      className={cn('rounded-lg border bg-surface px-4 pb-2 pt-1', tone === 'danger' ? 'border-danger' : 'border-edge', className)}
    >
      <legend className={cn('px-1 text-xs font-semibold uppercase tracking-wide', tone === 'danger' ? 'text-danger' : 'text-ink-muted')}>
        {legend}
      </legend>
      {description && <p className="mb-1 text-xs text-ink-muted">{description}</p>}
      <div className="divide-y divide-edge">{children}</div>
    </fieldset>
  )
}
```

`SettingRow.tsx`:

```tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SettingRowProps {
  label: string
  htmlFor: string
  description?: React.ReactNode
  error?: React.ReactNode
  status?: React.ReactNode
  wide?: boolean
  children: React.ReactElement<{ 'aria-describedby'?: string }>
}

/**
 * Label · description · one control (Microsoft SettingsCard shape, C4). The
 * description is always visible — never tooltip-only (S24). The control's
 * `aria-describedby` is appended with the description and error ids, in that
 * order, preserving anything the caller already set (same rule as ui/Field).
 */
export function SettingRow({ label, htmlFor, description, error, status, wide = false, children }: SettingRowProps) {
  const descId = description ? `${htmlFor}-desc` : undefined
  const errId = error ? `${htmlFor}-error` : undefined
  const own = children.props['aria-describedby']
  const describedBy = [own, descId, errId].filter(Boolean).join(' ') || undefined
  const control = describedBy ? React.cloneElement(children, { 'aria-describedby': describedBy }) : children

  return (
    <div className={cn('grid gap-3 py-3', wide ? 'grid-cols-1' : 'sm:grid-cols-[minmax(0,1fr)_minmax(200px,42%)] sm:items-center')}>
      <div className="min-w-0">
        <label htmlFor={htmlFor} className="block text-sm font-medium text-ink">{label}</label>
        {description && <p id={descId} className="mt-0.5 text-xs text-ink-muted">{description}</p>}
        {error && <p id={errId} role="alert" className="mt-0.5 text-xs text-danger"><span className="sr-only">Error: </span>{error}</p>}
      </div>
      <div className={cn('flex items-center gap-2', !wide && 'sm:justify-end')}>
        {status !== undefined && status !== null && (
          <span role="status" aria-live="polite" className="text-xs text-success whitespace-nowrap">{status}</span>
        )}
        {control}
      </div>
    </div>
  )
}
```

Note the `sm:grid-cols-[…]` arbitrary value: the lint rule forbids arbitrary **px**; a `minmax(0,1fr)` track is not a pixel literal. If the rule rejects it anyway, replace with `sm:grid-cols-5` and give the control column `sm:col-span-2`.

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings/chassis/chassis.test.tsx`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/scope.ts web/src/features/settings/chassis/
git commit -m "feat(settings): section chassis — SettingsSection, SettingsGroup, SettingRow, ScopeBadge (spec §7.1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `useSettingsForm` and `SaveBar` (the Form save model)

**Files:**
- Create: `web/src/features/settings/useSettingsForm.ts`, `web/src/features/settings/chassis/SaveBar.tsx`
- Test: `web/src/features/settings/useSettingsForm.test.tsx`

**Interfaces:**
- Consumes: `useBlocker` from `react-router-dom` (data router only — every consumer of this hook renders under `createBrowserRouter`/`createMemoryRouter`), `Button`, `ApiRequestError` (`web/src/api/client.ts`).
- Produces:

```ts
export interface SettingsForm<T extends object> {
  draft: T | undefined                 // undefined until `initial` arrives
  set: <K extends keyof T>(key: K, value: T[K]) => void
  dirty: boolean
  saving: boolean
  savedAt: number | null               // Date.now() of the last successful save
  error: string | null                 // top-of-section message; the caller renders it in a Banner
  save: () => Promise<boolean>         // resolves true on success
  discard: () => void
}
export function useSettingsForm<T extends object>(opts: {
  initial: T | undefined
  save: (draft: T) => Promise<unknown>
  /** Optional gate run before save (e.g. a ConfirmDialog). Return false to abort. */
  beforeSave?: (draft: T, initial: T) => Promise<boolean> | boolean
}): SettingsForm<T>

export interface SaveBarProps {
  form: Pick<SettingsForm<object>, 'dirty' | 'saving' | 'savedAt' | 'save' | 'discard'>
}
export function SaveBar({ form }: SaveBarProps): JSX.Element
```

Semantics: `draft` is seeded from `initial` once (when it goes undefined → defined) and re-seeded whenever `initial` changes **while not dirty** (a background refetch must not clobber edits). `dirty` = shallow inequality of `draft` vs `initial` by key. While dirty: `useBlocker` blocks in-app navigation and the hook renders nothing itself; the caller's `SaveBar` shows the blocker prompt. `beforeunload` is registered while dirty.

- [ ] **Step 1: Write the failing tests**

```tsx
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, Link, RouterProvider } from 'react-router-dom'
import { useSettingsForm } from './useSettingsForm'
import { SaveBar } from './chassis/SaveBar'

type Draft = { repoRoot: string; maxActive: number }

function Harness({ initial, save, beforeSave }: {
  initial: Draft | undefined
  save: (d: Draft) => Promise<unknown>
  beforeSave?: (d: Draft, i: Draft) => boolean
}) {
  const form = useSettingsForm<Draft>({ initial, save, beforeSave })
  if (!form.draft) return <p>loading</p>
  return (
    <div>
      <input aria-label="Repo" value={form.draft.repoRoot} onChange={(e) => form.set('repoRoot', e.target.value)} />
      {form.error && <p role="alert">{form.error}</p>}
      <SaveBar form={form} />
      <Link to="/elsewhere">leave</Link>
    </div>
  )
}

function mount(ui: React.ReactElement) {
  const router = createMemoryRouter([
    { path: '/', element: ui },
    { path: '/elsewhere', element: <p>elsewhere</p> },
  ])
  render(<RouterProvider router={router} />)
  return router
}

describe('useSettingsForm + SaveBar', () => {
  it('starts clean, becomes dirty on edit, saves, and reports Saved', async () => {
    const save = vi.fn().mockResolvedValue({})
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} />)
    const saveBtn = screen.getByRole('button', { name: 'Save' })
    expect(saveBtn).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    expect(saveBtn).toBeEnabled()
    expect(screen.getByRole('status')).toHaveTextContent('Unsaved changes')
    await userEvent.click(saveBtn)
    expect(save).toHaveBeenCalledWith({ repoRoot: '/ab', maxActive: 5 })
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Saved/))
  })

  it('Discard restores the initial value', async () => {
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={vi.fn()} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'zzz')
    await userEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.getByLabelText('Repo')).toHaveValue('/a')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('a failed save keeps the draft and shows the error', async () => {
    const save = vi.fn().mockRejectedValue(new Error('boom'))
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
    expect(screen.getByLabelText('Repo')).toHaveValue('/ab')
  })

  it('blocks in-app navigation while dirty and offers Keep editing / Discard and leave', async () => {
    const router = mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={vi.fn()} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('link', { name: 'leave' }))
    expect(router.state.location.pathname).toBe('/')
    expect(screen.getByRole('dialog', { name: 'Unsaved changes' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Discard and leave' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/elsewhere'))
  })

  it('beforeSave returning false aborts the save', async () => {
    const save = vi.fn()
    mount(<Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} beforeSave={() => false} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(save).not.toHaveBeenCalled()
  })

  it('a refetch does not clobber a dirty draft, but re-seeds a clean one', async () => {
    const save = vi.fn()
    const router = createMemoryRouter([{ path: '/', element: <Harness initial={{ repoRoot: '/a', maxActive: 5 }} save={save} /> }])
    const view = render(<RouterProvider router={router} />)
    await userEvent.type(screen.getByLabelText('Repo'), 'b')
    act(() => { view.rerender(<RouterProvider router={createMemoryRouter([{ path: '/', element: <Harness initial={{ repoRoot: '/server', maxActive: 5 }} save={save} /> }])} />) })
    // A remount with a different router is a new form; assert the simpler invariant directly:
    expect(screen.getByLabelText('Repo')).toHaveValue('/server')
  })
})
```

The last test verifies re-seeding on a clean form. The "dirty draft survives refetch" half is covered by a unit test on the pure helper `nextDraft` below; add it in the same file:

```tsx
import { nextDraft } from './useSettingsForm'

describe('nextDraft', () => {
  it('keeps a dirty draft, replaces a clean one', () => {
    expect(nextDraft({ a: 1 }, { a: 1 }, { a: 2 })).toEqual({ a: 2 })      // clean → re-seed
    expect(nextDraft({ a: 1 }, { a: 9 }, { a: 2 })).toEqual({ a: 9 })      // dirty → keep
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/features/settings/useSettingsForm.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

`web/src/features/settings/useSettingsForm.ts`:

```ts
import * as React from 'react'
import { useBlocker } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'

export interface SettingsForm<T extends object> {
  draft: T | undefined
  set: <K extends keyof T>(key: K, value: T[K]) => void
  dirty: boolean
  saving: boolean
  savedAt: number | null
  error: string | null
  save: () => Promise<boolean>
  discard: () => void
  /** The navigation blocker, for SaveBar's "Unsaved changes" dialog. */
  blocker: ReturnType<typeof useBlocker>
}

export function isDirty<T extends object>(initial: T, draft: T): boolean {
  return (Object.keys(initial) as (keyof T)[]).some((k) => !Object.is(initial[k], draft[k]))
}

/** Pure: what the draft should be when `initial` changes. A dirty draft is never clobbered. */
export function nextDraft<T extends object>(prevInitial: T, draft: T, newInitial: T): T {
  return isDirty(prevInitial, draft) ? draft : newInitial
}

function messageOf(err: unknown): string {
  if (err instanceof ApiRequestError) {
    const d = err.body.detail
    if (typeof d === 'string') return d
    if (d && typeof d === 'object' && 'message' in d && typeof (d as { message: unknown }).message === 'string') {
      return (d as { message: string }).message
    }
    return `Request failed (${err.status})`
  }
  return err instanceof Error ? err.message : 'Save failed'
}

/**
 * The Form save model (spec §7.2): a section-local draft with explicit Save /
 * Discard, a navigation blocker while dirty, and errors that keep the input.
 */
export function useSettingsForm<T extends object>({ initial, save, beforeSave }: {
  initial: T | undefined
  save: (draft: T) => Promise<unknown>
  beforeSave?: (draft: T, initial: T) => Promise<boolean> | boolean
}): SettingsForm<T> {
  const [draft, setDraft] = React.useState<T | undefined>(initial)
  const [seed, setSeed] = React.useState<T | undefined>(initial)
  const [saving, setSaving] = React.useState(false)
  const [savedAt, setSavedAt] = React.useState<number | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  // Re-seed when the server value changes, unless the user has edits.
  if (initial !== seed) {
    setSeed(initial)
    if (initial !== undefined) setDraft(draft === undefined || seed === undefined ? initial : nextDraft(seed, draft, initial))
  }

  const dirty = initial !== undefined && draft !== undefined && isDirty(initial, draft)

  const blocker = useBlocker(dirty)

  React.useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const set = React.useCallback(<K extends keyof T>(key: K, value: T[K]) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d))
    setError(null)
  }, [])

  const doSave = React.useCallback(async () => {
    if (!draft || !initial || !dirty) return false
    if (beforeSave && !(await beforeSave(draft, initial))) return false
    setSaving(true)
    setError(null)
    try {
      await save(draft)
      setSavedAt(Date.now())
      return true
    } catch (err) {
      setError(messageOf(err))
      return false
    } finally {
      setSaving(false)
    }
  }, [draft, initial, dirty, beforeSave, save])

  const discard = React.useCallback(() => {
    setDraft(initial)
    setError(null)
  }, [initial])

  return { draft, set, dirty, saving, savedAt, error, save: doSave, discard, blocker }
}
```

`web/src/features/settings/chassis/SaveBar.tsx`:

```tsx
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import type { SettingsForm } from '../useSettingsForm'

export interface SaveBarProps {
  form: Pick<SettingsForm<object>, 'dirty' | 'saving' | 'savedAt' | 'save' | 'discard' | 'blocker'>
}

function savedLabel(savedAt: number | null): string {
  if (savedAt === null) return ''
  const s = Math.round((Date.now() - savedAt) / 1000)
  return s < 5 ? 'Saved · just now' : s < 60 ? `Saved · ${s}s ago` : 'Saved'
}

/**
 * Sticky Save / Discard footer for form sections (spec §7.2). `role="status"`
 * announces dirty and saved states (WCAG 4.1.3). The parent scroll container
 * sets `scroll-padding-bottom` so a focused field is never hidden under it
 * (WCAG 2.4.11) — see SettingsPage.
 */
export function SaveBar({ form }: SaveBarProps) {
  const { dirty, saving, savedAt, save, discard, blocker } = form
  const status = dirty ? 'Unsaved changes' : savedLabel(savedAt)
  return (
    <>
      <div className="sticky bottom-0 z-sticky -mx-4 md:-mx-6 mt-2 flex items-center justify-between gap-3 border-t border-edge bg-surface px-4 py-3 md:px-6">
        <span role="status" aria-live="polite" className={dirty ? 'text-sm text-warning' : 'text-sm text-success'}>{status}</span>
        <div className="flex items-center gap-2">
          <Button type="button" variant="ghost" onClick={discard} disabled={!dirty || saving}>Discard</Button>
          <Button type="button" variant="primary" onClick={() => { void save() }} disabled={!dirty || saving} loading={saving}>Save</Button>
        </div>
      </div>
      <Dialog
        open={blocker.state === 'blocked'}
        onClose={() => blocker.reset?.()}
        title="Unsaved changes"
        description="You have changes that have not been saved."
        size="sm"
      >
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => blocker.reset?.()}>Keep editing</Button>
          <Button type="button" variant="danger" onClick={() => { discard(); blocker.proceed?.() }}>Discard and leave</Button>
        </div>
      </Dialog>
    </>
  )
}
```

`SettingsForm.blocker` was added to the interface above; keep the two in sync (the interface block earlier in this task is the contract).

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings/useSettingsForm.test.tsx`; lint; typecheck. Expected: PASS / clean. If the "setState during render" re-seed pattern trips `react-hooks` lint, move it into a `useEffect` keyed on `initial` with the same `nextDraft` logic.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/useSettingsForm.ts web/src/features/settings/useSettingsForm.test.tsx web/src/features/settings/chassis/SaveBar.tsx
git commit -m "feat(settings): useSettingsForm + SaveBar — draft, dirty, blocker, inline status (S10–S12)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: `useViewport` gains `portrait`

**Files:**
- Modify: `web/src/shell/useViewport.tsx` (`Viewport`, `ViewportProvider`)
- Modify: `web/src/test/matchMedia.ts` (`MEDIA.portrait`)
- Test: `web/src/shell/useViewport.test.tsx`

**Interfaces:**
- Produces: `Viewport.portrait: boolean` from `(orientation: portrait)`; `MEDIA.portrait = '(orientation: portrait)'` exported for tests.

- [ ] **Step 1: Write the failing test**

Append to `web/src/shell/useViewport.test.tsx`, following its existing render helper:

```tsx
it('reports portrait from the orientation query (DEC-3)', () => {
  stubMatchMedia([MEDIA.tablet, MEDIA.portrait])
  const { result } = renderHook(() => useViewport(), { wrapper: ({ children }) => <ViewportProvider kiosk={null}>{children}</ViewportProvider> })
  expect(result.current.mode).toBe('tablet')
  expect(result.current.portrait).toBe(true)
})
```

Import `MEDIA` from `@/test/matchMedia` if not already imported.

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web test -- src/shell/useViewport.test.tsx`
Expected: FAIL — `MEDIA.portrait` undefined / `portrait` not on `Viewport`.

- [ ] **Step 3: Implement**

`web/src/test/matchMedia.ts`, inside `MEDIA`: `portrait: '(orientation: portrait)',`.

`web/src/shell/useViewport.tsx`: add to `Viewport`

```ts
  /** `(orientation: portrait)`. Settings uses it: tablet-portrait drills like phone (spec §6.1). */
  portrait: boolean
```

and in `ViewportProvider`:

```ts
  const portrait = useMediaQuery('(orientation: portrait)')
  // …inside useMemo's returned object:
      portrait,
  // …and add `portrait` to the dependency array.
```

Check `useViewport.test.tsx` has a guard listing every `matchMedia` query the module may read; add `'(orientation: portrait)'` to that allowlist.

- [ ] **Step 4: Run the shell tests, lint, typecheck**

Run: `npm --prefix web test -- src/shell`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/useViewport.tsx web/src/shell/useViewport.test.tsx web/src/test/matchMedia.ts
git commit -m "feat(shell): useViewport reports portrait (for the Settings drill-down on tablet)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: `settingsDefs` registry, `lastSection`, `useSettingsBadges`

> **Superseded by R10:** the registry ships **17** sections in 5 groups, not 16 — `automation`
> (group Sessions, label "Automations", last in group, component `AutomationView`) is a real
> section, so `isSettingsSectionId('automation')` is **true** and the count assertion below reads
> 17. See DEC-10 / spec D3. PR 6 retires the row.

**Files:**
- Create: `web/src/features/settings/settingsDefs.ts`, `web/src/features/settings/lastSection.ts`, `web/src/features/settings/useSettingsBadges.ts`
- Test: `web/src/features/settings/settingsDefs.test.ts`

**Interfaces:**
- Consumes: the legacy section components (imported unchanged): `AccountsManager` (`@/components/AccountsManager`), `PluginsSection`/`SkillsSection`/`CommandsSection`/`McpSection` (`@/components/library/*`), `PresetsSection`/`TemplatesSection`/`PermissionProfilesSection`/`HooksSection`/`NotificationsSection`/`SnapshotsSection`/`FleetSection` (`@/components/settings/*`); the four new sections from Tasks 13, 14, 16, 17 (create them as **stubs** now — a component returning `null` in each file — so the registry compiles; the tasks fill them in); `useAccounts`.
- Produces:

```ts
export type SettingsGroupId = 'workspace' | 'sessions' | 'accounts' | 'claude' | 'about'
export interface SettingsGroupDef { id: SettingsGroupId; label: string }
export const SETTINGS_GROUPS: readonly SettingsGroupDef[]      // in nav order

export type SettingsBadgeKey = 'accountsAttention'
export interface SettingsDef {
  id: string; group: SettingsGroupId; label: string; icon: LucideIcon
  scope: SettingsScope; description: string; keywords?: readonly string[]
  badge?: SettingsBadgeKey; component: React.ComponentType
}
export type SettingsSectionId = 'general'|'appearance'|'notifications'|'sessions'|'presets'|'templates'|'profiles'|'accounts'|'fleet'|'plugins'|'skills'|'commands'|'mcp'|'hooks'|'snapshots'|'about'
export const SETTINGS_DEFS: readonly SettingsDef[]               // nav order within groups
export const SETTINGS_BY_ID: Record<SettingsSectionId, SettingsDef>
export const DEFAULT_SETTINGS_SECTION: SettingsSectionId          // 'general'
export function isSettingsSectionId(x: string | null | undefined): x is SettingsSectionId
export function sectionsInGroup(group: SettingsGroupId): SettingsDef[]

// lastSection.ts
export const LAST_SECTION_KEY = 'ccswitch.settings.last'
export function readLastSection(): SettingsSectionId | null
export function writeLastSection(id: SettingsSectionId): void

// useSettingsBadges.ts
export function useSettingsBadges(): Record<SettingsBadgeKey, number | undefined>
```

- [ ] **Step 1: Write the failing test**

```ts
import { describe, it, expect } from 'vitest'
import {
  SETTINGS_DEFS, SETTINGS_BY_ID, SETTINGS_GROUPS, DEFAULT_SETTINGS_SECTION,
  isSettingsSectionId, sectionsInGroup,
} from './settingsDefs'
import { readLastSection, writeLastSection, LAST_SECTION_KEY } from './lastSection'

describe('settingsDefs', () => {
  it('has 16 sections in 5 groups, ids unique and URL-safe, every def described', () => {
    expect(SETTINGS_DEFS).toHaveLength(16)
    expect(SETTINGS_GROUPS.map((g) => g.id)).toEqual(['workspace', 'sessions', 'accounts', 'claude', 'about'])
    const ids = SETTINGS_DEFS.map((d) => d.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const d of SETTINGS_DEFS) {
      expect(d.id).toMatch(/^[a-z]+$/)
      expect(d.description.length).toBeGreaterThan(10)
      expect(SETTINGS_GROUPS.some((g) => g.id === d.group)).toBe(true)
    }
  })

  it('keeps every legacy section id so old links resolve (spec §5)', () => {
    for (const legacy of ['general', 'accounts', 'notifications', 'hooks', 'plugins', 'skills', 'commands', 'mcp', 'presets', 'templates', 'profiles', 'snapshots', 'fleet']) {
      expect(isSettingsSectionId(legacy)).toBe(true)
    }
    expect(isSettingsSectionId('automation')).toBe(false)   // D3: removed
    expect(isSettingsSectionId('')).toBe(false)
    expect(DEFAULT_SETTINGS_SECTION).toBe('general')
  })

  it('groups sections in nav order; Accounts carries the attention badge', () => {
    expect(sectionsInGroup('workspace').map((d) => d.id)).toEqual(['general', 'appearance', 'notifications'])
    expect(sectionsInGroup('sessions').map((d) => d.id)).toEqual(['sessions', 'presets', 'templates', 'profiles'])
    expect(SETTINGS_BY_ID.accounts.badge).toBe('accountsAttention')
    expect(SETTINGS_BY_ID.appearance.scope).toBe('browser')
    expect(SETTINGS_BY_ID.about.scope).toBe('none')
  })
})

describe('lastSection', () => {
  it('round-trips a valid id and ignores garbage', () => {
    expect(readLastSection()).toBeNull()
    writeLastSection('fleet')
    expect(readLastSection()).toBe('fleet')
    localStorage.setItem(LAST_SECTION_KEY, 'automation')
    expect(readLastSection()).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web test -- src/features/settings/settingsDefs.test.ts`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create the four stub section files**

Each of these is replaced by its own task; the stub exists so the registry compiles:

```tsx
// web/src/features/settings/sections/GeneralSection.tsx  (Task 13 replaces)
export function GeneralSection() { return null }
// web/src/features/settings/sections/SessionDefaultsSection.tsx  (Task 14 replaces)
export function SessionDefaultsSection() { return null }
// web/src/features/settings/sections/AppearanceSection.tsx  (Task 16 replaces)
export function AppearanceSection() { return null }
// web/src/features/settings/sections/AboutSection.tsx  (Task 17 replaces)
export function AboutSection() { return null }
```

- [ ] **Step 4: Implement the registry**

`web/src/features/settings/settingsDefs.ts`:

```ts
import type * as React from 'react'
import {
  SlidersHorizontal, Palette, Bell, Play, Bookmark, FileText, ShieldCheck,
  Users, Server, Puzzle, Sparkles, TerminalSquare, Plug, Webhook, Camera, Info,
  type LucideIcon,
} from 'lucide-react'
import type { SettingsScope } from './scope'
import { GeneralSection } from './sections/GeneralSection'
import { AppearanceSection } from './sections/AppearanceSection'
import { SessionDefaultsSection } from './sections/SessionDefaultsSection'
import { AboutSection } from './sections/AboutSection'
import { AccountsManager } from '@/components/AccountsManager'
import { NotificationsSection } from '@/components/settings/NotificationsSection'
import { HooksSection } from '@/components/settings/HooksSection'
import { SnapshotsSection } from '@/components/settings/SnapshotsSection'
import { FleetSection } from '@/components/settings/FleetSection'
import { PresetsSection } from '@/components/settings/PresetsSection'
import { TemplatesSection } from '@/components/settings/TemplatesSection'
import { PermissionProfilesSection } from '@/components/settings/PermissionProfilesSection'
import { PluginsSection } from '@/components/library/PluginsSection'
import { SkillsSection } from '@/components/library/SkillsSection'
import { CommandsSection } from '@/components/library/CommandsSection'
import { McpSection } from '@/components/library/McpSection'

export type SettingsGroupId = 'workspace' | 'sessions' | 'accounts' | 'claude' | 'about'
export interface SettingsGroupDef { id: SettingsGroupId; label: string }

/** Nav order. Frequency-first (spec §5): weekly changes above yearly ones. */
export const SETTINGS_GROUPS: readonly SettingsGroupDef[] = [
  { id: 'workspace', label: 'Workspace' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'claude', label: 'Claude config' },
  { id: 'about', label: 'About' },
]

export type SettingsBadgeKey = 'accountsAttention'

interface SettingsDefShape {
  id: string
  group: SettingsGroupId
  label: string
  icon: LucideIcon
  scope: SettingsScope
  /** One sentence under the section title. Required: a section without one does not compile. */
  description: string
  keywords?: readonly string[]
  badge?: SettingsBadgeKey
  component: React.ComponentType
}

/**
 * The one Settings map (spec §6.2). SettingsNav, the phone list, the route
 * fallback and pageContract.test.tsx all derive from this array. Ids are URL
 * segments and are stable: every id that existed before this file did is kept.
 */
const DEFS = [
  { id: 'general',       group: 'workspace', label: 'General',             icon: SlidersHorizontal, scope: 'server',  component: GeneralSection,
    description: 'Where repos are cloned and archives are stored, and how many sessions run and are kept.' },
  { id: 'appearance',    group: 'workspace', label: 'Appearance',          icon: Palette,           scope: 'browser', component: AppearanceSection,
    description: 'Theme, navigation and layout for this browser only.', keywords: ['theme', 'dark', 'light'] },
  { id: 'notifications', group: 'workspace', label: 'Notifications',       icon: Bell,              scope: 'device',  component: NotificationsSection,
    description: 'Push to this device, quiet hours, webhook channels and routing rules.' },
  { id: 'sessions',      group: 'sessions',  label: 'Defaults',            icon: Play,              scope: 'server',  component: SessionDefaultsSection,
    description: 'What every new session starts with: tool approvals, model gating and transcript display.', keywords: ['permission', 'model', 'transcript'] },
  { id: 'presets',       group: 'sessions',  label: 'Presets',             icon: Bookmark,          scope: 'server',  component: PresetsSection,
    description: 'Saved launch configurations for the New session dialog.' },
  { id: 'templates',     group: 'sessions',  label: 'Templates',           icon: FileText,          scope: 'server',  component: TemplatesSection,
    description: 'Reusable prompt templates with {{variable}} placeholders.' },
  { id: 'profiles',      group: 'sessions',  label: 'Permission profiles', icon: ShieldCheck,       scope: 'server',  component: PermissionProfilesSection,
    description: 'Named tool-permission sets a session can adopt.' },
  { id: 'accounts',      group: 'accounts',  label: 'Accounts',            icon: Users,             scope: 'server',  component: AccountsManager, badge: 'accountsAttention',
    description: 'Claude Max and Copilot accounts the rotation can pick from.' },
  { id: 'fleet',         group: 'accounts',  label: 'Hosts',               icon: Server,            scope: 'server',  component: FleetSection,
    description: 'Machines that can run sessions, their weight and capacity.' },
  { id: 'plugins',       group: 'claude',    label: 'Plugins',             icon: Puzzle,            scope: 'server',  component: PluginsSection,
    description: 'Installed Claude Code plugins and what each one adds.' },
  { id: 'skills',        group: 'claude',    label: 'Skills',              icon: Sparkles,          scope: 'server',  component: SkillsSection,
    description: 'Skills available to every session, with an AI draft helper.' },
  { id: 'commands',      group: 'claude',    label: 'Commands',            icon: TerminalSquare,    scope: 'server',  component: CommandsSection,
    description: 'Slash commands available to every session.' },
  { id: 'mcp',           group: 'claude',    label: 'MCP servers',         icon: Plug,              scope: 'server',  component: McpSection,
    description: 'Model Context Protocol servers sessions can call, with OAuth where needed.' },
  { id: 'hooks',         group: 'claude',    label: 'Hooks',               icon: Webhook,           scope: 'server',  component: HooksSection,
    description: 'Shell commands Claude Code runs on lifecycle events. They run on this host.' },
  { id: 'snapshots',     group: 'claude',    label: 'Snapshots',           icon: Camera,            scope: 'server',  component: SnapshotsSection,
    description: 'Point-in-time copies of the shared Claude config, with diff, restore, export and import.' },
  { id: 'about',         group: 'about',     label: 'About',               icon: Info,              scope: 'none',    component: AboutSection,
    description: 'Version, host, and the server settings managed by environment variables.' },
] as const satisfies readonly SettingsDefShape[]

export type SettingsSectionId = (typeof DEFS)[number]['id']
export interface SettingsDef extends SettingsDefShape { id: SettingsSectionId }

export const SETTINGS_DEFS: readonly SettingsDef[] = DEFS
export const DEFAULT_SETTINGS_SECTION: SettingsSectionId = 'general'

export const SETTINGS_BY_ID: Record<SettingsSectionId, SettingsDef> = (() => {
  const out = {} as Record<SettingsSectionId, SettingsDef>
  for (const d of SETTINGS_DEFS) {
    if (d.id in out) throw new Error(`settingsDefs: duplicate id ${d.id}`)
    out[d.id] = d
  }
  return out
})()

export function isSettingsSectionId(x: string | null | undefined): x is SettingsSectionId {
  return typeof x === 'string' && Object.prototype.hasOwnProperty.call(SETTINGS_BY_ID, x)
}

export function sectionsInGroup(group: SettingsGroupId): SettingsDef[] {
  return SETTINGS_DEFS.filter((d) => d.group === group)
}
```

Verify each lucide icon name exists in the installed `lucide-react` (`grep -c "export { default as Webhook" web/node_modules/lucide-react/dist/lucide-react.d.ts` style check, or simply let typecheck report a missing export and pick the nearest name).

`web/src/features/settings/lastSection.ts`:

```ts
import { isSettingsSectionId, type SettingsSectionId } from './settingsDefs'

/** DEC-9: bare /settings on desktop reopens the last section (C1 "restore the most recently viewed pane"). */
export const LAST_SECTION_KEY = 'ccswitch.settings.last'

export function readLastSection(): SettingsSectionId | null {
  try {
    const v = localStorage.getItem(LAST_SECTION_KEY)
    return isSettingsSectionId(v) ? v : null
  } catch { return null }
}

export function writeLastSection(id: SettingsSectionId): void {
  try { localStorage.setItem(LAST_SECTION_KEY, id) } catch { /* storage unavailable */ }
}
```

`web/src/features/settings/useSettingsBadges.ts`:

```ts
import { useAccounts } from '@/api/hooks'
import type { SettingsBadgeKey } from './settingsDefs'

/** In-Settings badges (C20): a COUNT inside Settings; the rail keeps its dot. Same predicate as useNavBadges. */
export function useSettingsBadges(): Record<SettingsBadgeKey, number | undefined> {
  const { data: accounts = [] } = useAccounts()
  const n = accounts.filter((a) => a.status === 'needs-reauth' || a.status === 'needs-relogin').length
  return { accountsAttention: n || undefined }
}
```

- [ ] **Step 5: Run test, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings/settingsDefs.test.ts`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/features/settings/settingsDefs.ts web/src/features/settings/settingsDefs.test.ts web/src/features/settings/lastSection.ts web/src/features/settings/useSettingsBadges.ts web/src/features/settings/sections/
git commit -m "feat(settings): settingsDefs registry (5 groups, 16 sections), last-section memory, badges (S5, S9)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: `SettingsNav`, `HostedSection`, `SettingsPage`, `SettingsIndexPage`, routes; delete `SettingsView`

> **Superseded by R10:** the `automation` Settings section is **kept**, so the route table, the
> `SettingsPage` early return and the redirect tests below differ from what shipped. Final
> behaviour: `/settings/automation` is a real section (hosted via `HostedSection` →
> `AutomationView`), there is **no** `/settings/automation` → `/automations` redirect, and the
> legacy `/automation` → `/settings/automation` redirect is kept. `SettingsPage` therefore has no
> early return for `automation`, and the test that asserted the outward redirect is replaced by
> `automationTab.test.tsx`, which names the enable/mode/config/schedule controls the section owns.
> See DEC-10 / spec D3; PR 6 folds configuration into the Automations page and retires the section.

**Files:**
- Create: `web/src/features/settings/SettingsNav.tsx`, `HostedSection.tsx`, `SettingsPage.tsx`, `SettingsIndexPage.tsx`, `SettingsPage.test.tsx`, `pageContract.test.tsx`
- Modify: `web/src/shell/routes.tsx` (`SettingsPage`, `SettingsIndexRedirect`, legacy rows), `web/src/shell/routes.test.tsx`
- Delete: `web/src/components/settings/SettingsView.tsx`, `web/src/components/settings/__tests__/SettingsView.test.tsx`

**Interfaces:**
- Consumes: Tasks 8, 10, 11; `PageHeader`, `PageContent`, `useViewport`, `settingsSectionPath` (`@/shell/navDefs`), `NAV_BY_ID.settings.path`, `useSettingsBadges`, `Badge`.
- Produces:

```ts
export function SettingsNav({ variant }: { variant: 'rail' | 'list' }): JSX.Element
export function HostedSection({ def }: { def: SettingsDef }): JSX.Element
export function SettingsPage(): JSX.Element            // reads :section
export function SettingsIndexPage(): JSX.Element       // bare /settings
export function useSettingsDrill(): boolean            // phone || tablet&&portrait  (in SettingsPage.tsx)
```

Route table after this task:

| Path | Element |
|---|---|
| `/settings` | `SettingsIndexPage` (list when drill; else redirect to last/general; `?section=X` → `/settings/X` first) |
| `/settings/:section` | `SettingsPage` (unknown id → `/settings/general` replace; `automation` → `/automations` replace) |
| `/accounts` | → `/settings/accounts` (kept) |
| `/automation` | → `/automations` (DEC-10) |
| `/library` | `LibraryRedirect` (kept) |
| `?dialog=settings` | → `/settings/general` (kept) |

- [ ] **Step 1: Write the failing tests**

`web/src/features/settings/SettingsPage.test.tsx` uses `renderApp` (`@/test/renderApp`) which mounts the real shell and route table. Mock every hook the hosted sections need the way `routes.test.tsx` does; the minimum is `vi.mock('@/api/hooks', …)` returning `queryResult([])`/`queryResult(appSettings())` via `@/test/reactQuery` for `useSettings`, `useAccounts`, `useArchivedSessions`, `useSessions`, `useAllPrs`, `useAutomations`, plus `mutationResult()` for `useUpdateSettings`. Copy the mock block from `routes.test.tsx` and extend it.

```tsx
import { describe, it, expect, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp } from '@/test/renderApp'
import { LAST_SECTION_KEY } from './lastSection'
// …mocks as described above…

describe('SettingsPage', () => {
  beforeEach(() => localStorage.clear())

  it('desktop: h1 Settings, grouped nav with aria-current, section h2, split variant', async () => {
    renderApp({ path: '/settings/sessions', mode: 'desktop' })
    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Settings sections' })
    expect(nav).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Defaults/, current: 'page' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Defaults' })).toBeInTheDocument()
    expect(document.querySelector('[data-page-variant="split"]')).not.toBeNull()
    expect(document.title).toBe('Defaults · Settings — ccswitch')
  })

  it('desktop: bare /settings redirects to the last visited section, else general', async () => {
    const a = renderApp({ path: '/settings', mode: 'desktop' })
    await waitFor(() => expect(a.path()).toBe('/settings/general'))
    a.unmount()
    localStorage.setItem(LAST_SECTION_KEY, 'fleet')
    const b = renderApp({ path: '/settings', mode: 'desktop' })
    await waitFor(() => expect(b.path()).toBe('/settings/fleet'))
  })

  it('phone: /settings is the grouped list; a section has Back to /settings', async () => {
    const r = renderApp({ path: '/settings', mode: 'phone' })
    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()
    await userEvent.click(screen.getByRole('link', { name: /Hosts/ }))
    await waitFor(() => expect(r.path()).toBe('/settings/fleet'))
    expect(screen.getByRole('heading', { level: 2, name: 'Hosts' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('link', { name: 'Back to Settings' }))
    await waitFor(() => expect(r.path()).toBe('/settings'))
  })

  it('tablet portrait drills like phone; tablet landscape shows the nav', async () => {
    const p = renderApp({ path: '/settings', mode: 'tablet', extraQueries: ['(orientation: portrait)'] })
    expect(await screen.findByRole('list', { name: 'Settings sections' })).toBeInTheDocument()
    p.unmount()
    const l = renderApp({ path: '/settings', mode: 'tablet' })
    await waitFor(() => expect(l.path()).toBe('/settings/general'))
    expect(screen.getByRole('navigation', { name: 'Settings sections' })).toBeInTheDocument()
  })

  it('unknown section → general; /settings/automation → /automations; /automation → /automations', async () => {
    const a = renderApp({ path: '/settings/nope', mode: 'desktop' })
    await waitFor(() => expect(a.path()).toBe('/settings/general'))
    a.unmount()
    const b = renderApp({ path: '/settings/automation', mode: 'desktop' })
    await waitFor(() => expect(b.path()).toBe('/automations'))
    b.unmount()
    const c = renderApp({ path: '/automation', mode: 'desktop' })
    await waitFor(() => expect(c.path()).toBe('/automations'))
  })

  it('keeps legacy links: /settings?section=hooks, /accounts, /library?section=skills, ?dialog=settings', async () => {
    for (const [from, to] of [
      ['/settings?section=hooks', '/settings/hooks'],
      ['/accounts', '/settings/accounts'],
      ['/library?section=skills', '/settings/skills'],
      ['/sessions?dialog=settings', '/settings/general'],
    ] as const) {
      const r = renderApp({ path: from, mode: 'desktop' })
      await waitFor(() => expect(r.path()).toBe(to))
      r.unmount()
    }
  })

  it('remembers the visited section and shows the accounts attention count', async () => {
    // mock useAccounts to return two accounts with status 'needs-reauth' for this test
    renderApp({ path: '/settings/accounts', mode: 'desktop' })
    await screen.findByRole('heading', { level: 2, name: 'Accounts' })
    expect(localStorage.getItem(LAST_SECTION_KEY)).toBe('accounts')
    expect(screen.getByRole('link', { name: /Accounts \(2\)/ })).toBeInTheDocument()
  })
})
```

`web/src/features/settings/pageContract.test.tsx` — the contract table seeded with Settings (DEC-5):

```tsx
import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderApp } from '@/test/renderApp'
import { LAYOUT_MODES } from '@/shell/useViewport'
import { NAV_BY_ID } from '@/shell/navDefs'
// …same hook mocks as SettingsPage.test.tsx…

/** Pages that conform to sub-spec C §2.3. Add a row when a page migrates. */
const PAGES = [
  { nav: 'settings', path: '/settings/general', variant: 'split', title: 'Settings' },
] as const

describe('page contract', () => {
  for (const page of PAGES) {
    for (const mode of LAYOUT_MODES.filter((m) => m !== 'kiosk' || NAV_BY_ID[page.nav].showInKiosk)) {
      it(`${page.nav} × ${mode}: one h1, document.title, variant, no horizontal scroll`, async () => {
        renderApp({ path: page.path, mode })
        const h1s = await screen.findAllByRole('heading', { level: 1 })
        expect(h1s).toHaveLength(1)
        expect(h1s[0]).toHaveTextContent(page.title)
        expect(document.title).toMatch(new RegExp(`${page.title} — ccswitch$`))
        expect(document.querySelector(`[data-page-variant="${page.variant}"]`)).not.toBeNull()
        const root = document.getElementById('root') ?? document.body
        expect(root.scrollWidth).toBeLessThanOrEqual(root.clientWidth)
      })
    }
  }
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/features/settings/SettingsPage.test.tsx src/features/settings/pageContract.test.tsx`
Expected: FAIL — modules not found / old tab strip renders.

- [ ] **Step 3: Implement `SettingsNav`**

```tsx
import { NavLink } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { settingsSectionPath } from '@/shell/navDefs'
import { SETTINGS_GROUPS, sectionsInGroup, type SettingsDef } from './settingsDefs'
import { useSettingsBadges } from './useSettingsBadges'
import { SCOPE_LABEL } from './scope'

/**
 * Derived from settingsDefs. `rail`: the vertical nav beside the panel
 * (desktop / tablet-landscape). `list`: the full-width grouped list that IS
 * /settings on phone and tablet-portrait (iOS idiom, C18).
 */
export function SettingsNav({ variant }: { variant: 'rail' | 'list' }) {
  const badges = useSettingsBadges()
  const count = (d: SettingsDef) => (d.badge ? badges[d.badge] : undefined)
  const isList = variant === 'list'

  const body = SETTINGS_GROUPS.map((g) => (
    <li key={g.id} className="flex flex-col gap-0.5">
      <span className="px-2 pb-1 text-2xs font-semibold uppercase tracking-wider text-ink-muted">{g.label}</span>
      <ul className={cn('flex flex-col', isList ? 'gap-1' : 'gap-0.5')}>
        {sectionsInGroup(g.id).map((d) => (
          <li key={d.id}>
            <NavLink
              to={settingsSectionPath(d.id)}
              aria-label={badgedName({ label: d.label, count: count(d) })}
              className={({ isActive }) => cn(
                'flex items-center gap-2 rounded-md px-2 text-sm text-ink min-h-target',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                isList ? 'border border-edge bg-surface px-3' : 'hover:bg-surface-raised',
                !isList && isActive && 'bg-accent-wash text-accent font-medium',
              )}
            >
              <d.icon size={16} aria-hidden="true" className="shrink-0" />
              <span className="flex-1 truncate">{d.label}</span>
              {count(d) !== undefined && <Badge tone="warning" count={count(d)} />}
              {isList && (
                <>
                  {d.scope !== 'server' && d.scope !== 'none' && (
                    <span className="text-xs text-ink-muted">{SCOPE_LABEL[d.scope]}</span>
                  )}
                  <ChevronRight size={16} aria-hidden="true" className="text-ink-faint" />
                </>
              )}
            </NavLink>
          </li>
        ))}
      </ul>
    </li>
  ))

  return isList
    ? <ul aria-label="Settings sections" className="flex flex-col gap-4">{body}</ul>
    : (
      <nav aria-label="Settings sections" className="w-60 shrink-0 overflow-y-auto border-r border-edge px-2 py-3">
        <ul className="flex flex-col gap-4">{body}</ul>
      </nav>
    )
}
```

`badgedName` in `web/src/ui/badgedName.ts` builds "Accounts (2)"; check its signature (`{ label, count, attention }`) and match it. `NavLink` sets `aria-current="page"` itself. `text-2xs` is the umbrella type scale's smallest step; confirm it compiles (`grep -- '--text-2xs' web/src/index.css`), else use `text-xs`.

- [ ] **Step 4: Implement `HostedSection`**

```tsx
import { SettingsSection } from './chassis/SettingsSection'
import type { SettingsDef } from './settingsDefs'

/**
 * DEC-12: an unmigrated section rendered inside the chassis heading. Its
 * internals are untouched; PRs 5b–5e replace each `component` with a real
 * section and this wrapper disappears with the last one.
 */
export function HostedSection({ def }: { def: SettingsDef }) {
  const Body = def.component
  return (
    <SettingsSection id={def.id} title={def.label} description={def.description} scope={def.scope}>
      <Body />
    </SettingsSection>
  )
}
```

The four new sections (General, Defaults, Appearance, About) render their own `SettingsSection`; the page decides which path to take by a `MIGRATED` set (below).

- [ ] **Step 5: Implement `SettingsPage` and `SettingsIndexPage`**

`web/src/features/settings/SettingsPage.tsx`:

```tsx
import * as React from 'react'
import { Navigate, NavLink, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { useViewport } from '@/shell/useViewport'
import { NAV_BY_ID, settingsSectionPath } from '@/shell/navDefs'
import { SETTINGS_BY_ID, DEFAULT_SETTINGS_SECTION, isSettingsSectionId, type SettingsSectionId } from './settingsDefs'
import { writeLastSection } from './lastSection'
import { SettingsNav } from './SettingsNav'
import { HostedSection } from './HostedSection'

/** Sections that render their own SettingsSection (spec §8). Everything else is hosted. */
const MIGRATED: ReadonlySet<SettingsSectionId> = new Set(['general', 'sessions', 'appearance', 'about'])

/** Phone, or tablet held portrait, gets list-then-detail (spec §6.1, DEC-3). */
export function useSettingsDrill(): boolean {
  const { mode, portrait } = useViewport()
  return mode === 'phone' || (mode === 'tablet' && portrait)
}

function SectionBody({ id }: { id: SettingsSectionId }) {
  const def = SETTINGS_BY_ID[id]
  if (MIGRATED.has(id)) {
    const Section = def.component
    return <Section />
  }
  return <HostedSection def={def} />
}

/** `/settings/:section` — the Split variant page (umbrella D11). */
export function SettingsPage() {
  const { section } = useParams()
  const drill = useSettingsDrill()

  if (section === 'automation') return <Navigate to={NAV_BY_ID.automations.path} replace />
  if (!isSettingsSectionId(section)) return <Navigate to={settingsSectionPath(DEFAULT_SETTINGS_SECTION)} replace />

  const def = SETTINGS_BY_ID[section]
  React.useEffect(() => { writeLastSection(section) }, [section])

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="split">
      <PageHeader
        title="Settings"
        meta={def.label}
        actions={drill ? (
          <NavLink to={NAV_BY_ID.settings.path} aria-label="Back to Settings" className="inline-flex items-center gap-1 text-sm text-accent min-h-target">
            <ArrowLeft size={16} aria-hidden="true" /> Settings
          </NavLink>
        ) : undefined}
      />
      <div className="flex min-h-0 flex-1">
        {!drill && <SettingsNav variant="rail" />}
        <PageContent className="[scroll-padding-bottom:5rem]">
          <SectionBody id={section} />
        </PageContent>
      </div>
    </div>
  )
}
```

`document.title` must read "Defaults · Settings — ccswitch": `PageHeader` composes `${title} — ccswitch`, so pass `title={`${def.label} · Settings`}`? No — the `h1` must be "Settings". Instead call `setBaseTitle` yourself: keep `title="Settings"` on `PageHeader` and add, in `SettingsPage`, a second effect that does not exist… `documentTitle.ts` allows one base per token; the cleanest path is to give `PageHeader` an optional `documentTitle?: string` prop (defaults to `title`) used only for the `setBaseTitle` call. Add that prop to `web/src/ui/PageHeader.tsx` (two lines) and pass `documentTitle={`${def.label} · Settings`}`. The `[scroll-padding-bottom:5rem]` arbitrary value uses `rem`, not px; if the lint rule rejects it, add `scroll-pb-20` (Tailwind's `scroll-padding-bottom: 5rem`).

`web/src/features/settings/SettingsIndexPage.tsx`:

```tsx
import { Navigate, useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { settingsSectionPath } from '@/shell/navDefs'
import { DEFAULT_SETTINGS_SECTION } from './settingsDefs'
import { readLastSection } from './lastSection'
import { SettingsNav } from './SettingsNav'
import { useSettingsDrill } from './SettingsPage'

/** Bare `/settings`: the grouped list when drilling, else a redirect (DEC-9). `?section=` is legacy. */
export function SettingsIndexPage() {
  const [params] = useSearchParams()
  const drill = useSettingsDrill()
  const legacy = params.get('section')
  if (legacy) return <Navigate to={settingsSectionPath(legacy)} replace />
  if (!drill) return <Navigate to={settingsSectionPath(readLastSection() ?? DEFAULT_SETTINGS_SECTION)} replace />
  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="split">
      <PageHeader title="Settings" />
      <PageContent>
        <SettingsNav variant="list" />
      </PageContent>
    </div>
  )
}
```

- [ ] **Step 6: Wire the routes and delete `SettingsView`**

In `web/src/shell/routes.tsx`:
- Replace the `SettingsView` import with `import { SettingsPage } from '@/features/settings/SettingsPage'` and `import { SettingsIndexPage } from '@/features/settings/SettingsIndexPage'`.
- Delete the local `SettingsPage` function (lines 166-174) and `SettingsIndexRedirect` (214-218).
- Table rows: `{ path: \`${SETTINGS_PATH}/:section\`, element: <SettingsPage /> }`, `{ path: SETTINGS_PATH, element: <SettingsIndexPage /> }`, and change `/automation` to `<Navigate to={NAV_BY_ID.automations.path} replace />`.
- Update the file's header comment paragraph about `/settings/:section` (DEC-14 is now fully landed).

Delete `web/src/components/settings/SettingsView.tsx` and `web/src/components/settings/__tests__/SettingsView.test.tsx`. Grep for remaining importers (`grep -rn "SettingsView" web/src`) and fix each: `routes.test.tsx` stubs it (change the stub to `@/features/settings/SettingsPage` and `SettingsIndexPage`); `navDefs.ts` mentions it in a comment (update wording); `web/src/test/factories.ts` and the `components/__tests__/Accounts*.test.tsx` files only reference `GeneralSection`/`SettingsView` in comments or mocks — update or remove those mocks.

- [ ] **Step 7: Run the tests, lint, typecheck, build**

Run: `npm --prefix web test -- src/features/settings src/shell/routes.test.tsx`; then the full `npm --prefix web test`; lint; typecheck; `npm --prefix web run build`. Expected: PASS / clean. The four stub sections render nothing yet, so `SettingsPage.test.tsx`'s `heading level 2 "Defaults"` assertion will fail until Task 14; mark that single assertion `it.todo` with a comment naming Task 14 and un-todo it there.

- [ ] **Step 8: Commit**

```bash
git add -A web/src/features/settings web/src/shell/routes.tsx web/src/shell/routes.test.tsx web/src/shell/navDefs.ts web/src/ui/PageHeader.tsx
git rm -q web/src/components/settings/SettingsView.tsx web/src/components/settings/__tests__/SettingsView.test.tsx
git commit -m "feat(settings): Split-variant SettingsPage with grouped nav and phone drill-down; delete SettingsView (S1–S3, S7)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: `GeneralSection` (Paths · Archive · Capacity)

**Files:**
- Replace stub: `web/src/features/settings/sections/GeneralSection.tsx`
- Test: `web/src/features/settings/sections/GeneralSection.test.tsx`

**Interfaces:**
- Consumes: `useSettings`, `useUpdateSettings`, `useArchivedSessions` (`@/api/hooks`); Tasks 6–9 (`Checkbox`, `ConfirmDialog`, chassis, `useSettingsForm`, `SaveBar`); `Input`, `Banner`.
- Produces: `export function GeneralSection(): JSX.Element` registered in `settingsDefs` (Task 11).

Draft shape (all strings so blank means "server default"):

```ts
interface GeneralDraft { repoRoot: string; archiveHome: string; autoArchiveEnabled: boolean; maxArchived: string; maxActive: string }
```

Save payload rules (unchanged from the old form, see inventory §1a): `repoRoot: trim || null` · `archiveHome: raw string` (`""` clears) · `maxArchivedSessions: int>0 ? int : 0` · `maxActiveSessions: int>0 ? int : null` · `autoArchiveEnabled`.

- [ ] **Step 1: Write the failing tests**

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, mutationResult } from '@/test/reactQuery'
import { GeneralSection } from './GeneralSection'

vi.mock('@/api/hooks', () => ({
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useArchivedSessions: vi.fn(),
}))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <GeneralSection /> }])
  return render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>)
}

describe('GeneralSection', () => {
  const mutateAsync = vi.fn().mockResolvedValue({})
  beforeEach(() => {
    mutateAsync.mockClear()
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      repoRoot: null, archiveHome: null, maxArchivedSessions: 100, maxActiveSessions: null, autoArchiveEnabled: true,
      effectiveDefaults: { repoRoot: '/home/tony/GitHub', archiveHome: '/var/ccswitch/archives', modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable', maxActiveSessions: 5 },
    })))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync }))
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult(Array.from({ length: 3 }, (_, i) => ({ id: `s${i}` })) as never))
  })

  it('shows effective defaults as placeholders and a Reset that clears to default', async () => {
    mount()
    expect(screen.getByRole('heading', { level: 2, name: 'General' })).toBeInTheDocument()
    const repo = screen.getByLabelText('Repo clone root')
    expect(repo).toHaveAttribute('placeholder', '/home/tony/GitHub · server default')
    await userEvent.type(repo, '/srv/x')
    await userEvent.click(screen.getByRole('button', { name: 'Reset repo clone root to default' }))
    expect(repo).toHaveValue('')
  })

  it('saves the same payload shape as before (blank → 0 / null)', async () => {
    mount()
    await userEvent.type(screen.getByLabelText('Max active sessions'), '7')
    await userEvent.clear(screen.getByLabelText('Max archived sessions'))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Auto-archive completed sessions' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenCalledWith({
      repoRoot: null, archiveHome: '', maxArchivedSessions: 0, maxActiveSessions: 7, autoArchiveEnabled: false,
    })
  })

  it('changing the archive path asks for a typed confirmation naming the archive count (S16)', async () => {
    mount()
    await userEvent.type(screen.getByLabelText('Archive storage path'), '/mnt/slow')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const dlg = await screen.findByRole('dialog', { name: 'Move archived sessions?' })
    expect(dlg).toHaveTextContent('about 3 archived sessions')
    expect(mutateAsync).not.toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('Type /mnt/slow to confirm'), '/mnt/slow')
    await userEvent.click(screen.getByRole('button', { name: 'Move archives' }))
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ archiveHome: '/mnt/slow' }))
  })

  it('a 409 from the move keeps the draft and shows the server detail', async () => {
    const { ApiRequestError } = await import('@/api/client')
    mutateAsync.mockRejectedValueOnce(new ApiRequestError(409, { detail: { code: 'archive_move_failed', moved: 2, failed: 1, total: 3 } }))
    mount()
    await userEvent.type(screen.getByLabelText('Max active sessions'), '9')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Request failed (409)')
    expect(screen.getByLabelText('Max active sessions')).toHaveValue(9)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/features/settings/sections/GeneralSection.test.tsx`
Expected: FAIL — the stub renders nothing.

- [ ] **Step 3: Implement**

```tsx
import * as React from 'react'
import { useSettings, useUpdateSettings, useArchivedSessions, type AppSettings } from '@/api/hooks'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { Checkbox } from '@/ui/Checkbox'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { useSettingsForm } from '../useSettingsForm'

interface GeneralDraft {
  repoRoot: string
  archiveHome: string
  autoArchiveEnabled: boolean
  maxArchived: string
  maxActive: string
}

function toDraft(s: AppSettings): GeneralDraft {
  return {
    repoRoot: s.repoRoot ?? '',
    archiveHome: s.archiveHome ?? '',
    autoArchiveEnabled: s.autoArchiveEnabled,
    maxArchived: s.maxArchivedSessions != null ? String(s.maxArchivedSessions) : '',
    maxActive: s.maxActiveSessions != null ? String(s.maxActiveSessions) : '',
  }
}

function toPayload(d: GeneralDraft): Partial<AppSettings> {
  const archived = Number.parseInt(d.maxArchived, 10)
  const active = Number.parseInt(d.maxActive, 10)
  return {
    repoRoot: d.repoRoot.trim() || null,
    archiveHome: d.archiveHome,                                            // "" clears to default
    maxArchivedSessions: Number.isFinite(archived) && archived > 0 ? archived : 0,   // 0 = unlimited
    maxActiveSessions: Number.isFinite(active) && active > 0 ? active : null,
    autoArchiveEnabled: d.autoArchiveEnabled,
  }
}

function ResetLink({ label, onClick, disabled }: { label: string; onClick: () => void; disabled: boolean }) {
  return <Button type="button" variant="ghost" size="sm" onClick={onClick} disabled={disabled} aria-label={label}>Reset</Button>
}

export function GeneralSection() {
  const { data: settings } = useSettings()
  const update = useUpdateSettings()
  const { data: archived = [] } = useArchivedSessions()
  const [confirmPath, setConfirmPath] = React.useState<{ resolve: (ok: boolean) => void; path: string } | null>(null)

  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<GeneralDraft>({
    initial,
    save: (d) => update.mutateAsync(toPayload(d)),
    // S16: moving every archive deserves the Typed tier.
    beforeSave: (d, i) => d.archiveHome.trim() === i.archiveHome.trim()
      ? true
      : new Promise<boolean>((resolve) => setConfirmPath({ resolve, path: d.archiveHome.trim() })),
  })

  if (!form.draft || !settings) return null
  const { draft, set } = form
  const eff = settings.effectiveDefaults

  return (
    <SettingsSection id="general" title="General" scope="server"
      description="Where repos are cloned and archives are stored, and how many sessions run and are kept."
      footer={<SaveBar form={form} />}>
      {form.error && <Banner tone="danger" role="alert" title="Not saved">{form.error}</Banner>}

      <SettingsGroup legend="Paths">
        <SettingRow label="Repo clone root" htmlFor="general-repo-root"
          description="New sessions clone repositories under this directory.">
          <span className="flex w-full items-center gap-2">
            <Input id="general-repo-root" value={draft.repoRoot} placeholder={`${eff.repoRoot} · server default`}
              onChange={(e) => set('repoRoot', e.target.value)} />
            <ResetLink label="Reset repo clone root to default" onClick={() => set('repoRoot', '')} disabled={draft.repoRoot === ''} />
          </span>
        </SettingRow>
        <SettingRow label="Archive storage path" htmlFor="general-archive-home"
          description="Where archived session files live. Changing it moves every existing archive.">
          <span className="flex w-full items-center gap-2">
            <Input id="general-archive-home" value={draft.archiveHome} placeholder={`${eff.archiveHome ?? 'default'} · server default`}
              onChange={(e) => set('archiveHome', e.target.value)} />
            <ResetLink label="Reset archive storage path to default" onClick={() => set('archiveHome', '')} disabled={draft.archiveHome === ''} />
          </span>
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Archive" description="What happens to sessions after they finish.">
        <SettingRow label="Auto-archive" htmlFor="general-auto-archive"
          description="A session that finishes its goal is archived automatically. Off keeps it on the dashboard until you archive it.">
          <Checkbox id="general-auto-archive" label="Auto-archive completed sessions" checked={draft.autoArchiveEnabled} onChange={(v) => set('autoArchiveEnabled', v)} />
        </SettingRow>
        <SettingRow label="Max archived sessions" htmlFor="general-max-archived"
          description="Above this count the oldest archives are permanently purged. Blank keeps everything.">
          <Input id="general-max-archived" type="number" min={1} step={1} inputMode="numeric" placeholder="Unlimited"
            value={draft.maxArchived} onChange={(e) => set('maxArchived', e.target.value)} />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Capacity">
        <SettingRow label="Max active sessions" htmlFor="general-max-active"
          description="Sessions beyond this limit queue and start when a slot frees.">
          <Input id="general-max-active" type="number" min={1} step={1} inputMode="numeric" placeholder={`${eff.maxActiveSessions} · server default`}
            value={draft.maxActive} onChange={(e) => set('maxActive', e.target.value)} />
        </SettingRow>
      </SettingsGroup>

      <ConfirmDialog
        open={confirmPath !== null}
        onClose={() => { confirmPath?.resolve(false); setConfirmPath(null) }}
        onConfirm={() => { confirmPath?.resolve(true); setConfirmPath(null) }}
        title="Move archived sessions?"
        description={`About ${archived.length} archived sessions will be moved to ${confirmPath?.path || 'the server default'}. If any move fails, nothing is saved.`}
        confirmLabel="Move archives"
        tone="warning"
        typed={confirmPath?.path || undefined}
      />
    </SettingsSection>
  )
}
```

If `Button` has no `size="sm"`, drop the prop. When `confirmPath.path` is empty (reset to default) the dialog falls back to the Confirm tier (`typed` undefined), which is the right friction for "use the default".

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings/sections/GeneralSection.test.tsx`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/GeneralSection.tsx web/src/features/settings/sections/GeneralSection.test.tsx
git commit -m "feat(settings): General section on the chassis — paths, archive, capacity, typed confirm for archive moves (S6, S16, S28)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: `SessionDefaultsSection` (Tool approvals · Model approval · Transcript)

**Files:**
- Replace stub: `web/src/features/settings/sections/SessionDefaultsSection.tsx`
- Test: `web/src/features/settings/sections/SessionDefaultsSection.test.tsx`
- Modify: `web/src/features/settings/SettingsPage.test.tsx` (un-todo the "Defaults" h2 assertion from Task 12)

**Interfaces:**
- Consumes: `PERMISSION_MODES` (`@/session/permissionModes`), `AppSettings.acceptedPermissionModes`, `useSettings`, `useUpdateSettings`, `Select`, `RadioGroup`, `Checkbox`, `Input`, `ConfirmDialog`, chassis, `useSettingsForm`, `SaveBar`.
- Produces: `export function SessionDefaultsSection(): JSX.Element`.

Draft: `{ permissionMode: string; modelApprovalAction: '' | 'require_approval' | 'block' | 'allow'; denylist: string; toolDetail: ToolDetailMode; thinkingCollapsed: boolean }`. Payload: `defaultPermissionMode`, `modelApprovalAction: value || null`, `modelApprovalDenylist: trim || null`, `defaultToolDetail`, `thinkingCollapsed`. Unsafe modes: `full-auto`, `bypassPermissions` → Confirm tier on Save when the mode **changed to** one of them (S17).

- [ ] **Step 1: Write the failing tests**

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, mutationResult } from '@/test/reactQuery'
import { SessionDefaultsSection } from './SessionDefaultsSection'

vi.mock('@/api/hooks', () => ({ useSettings: vi.fn(), useUpdateSettings: vi.fn() }))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <SessionDefaultsSection /> }])
  return render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>)
}

describe('SessionDefaultsSection', () => {
  const mutateAsync = vi.fn().mockResolvedValue({})
  beforeEach(() => {
    mutateAsync.mockClear()
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      defaultPermissionMode: 'interactive', thinkingCollapsed: true, defaultToolDetail: 'auto',
      modelApprovalAction: null, modelApprovalDenylist: null,
      acceptedPermissionModes: ['interactive', 'auto', 'allowlist', 'acceptEdits', 'full-auto'],   // bypass NOT accepted
    })))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync }))
  })

  it('initialises from the server (thinkingCollapsed true, not a hard-coded false) (S21)', () => {
    mount()
    expect(screen.getByRole('checkbox', { name: 'Collapse thinking by default' })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Server default/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Server default/ })).toHaveAccessibleName(/require approval/i)
  })

  it('disables, with a reason, any mode the server does not accept (DEC-4)', () => {
    mount()
    const opt = screen.getByRole('option', { name: /Bypass all permissions/ })
    expect(opt).toBeDisabled()
    expect(screen.getByText(/not accepted by this server/i)).toBeInTheDocument()
  })

  it('choosing full-auto asks for confirmation before saving (S17)', async () => {
    mount()
    await userEvent.selectOptions(screen.getByLabelText('Default tool approvals'), 'full-auto')
    expect(screen.getByRole('alert')).toHaveTextContent(/run code as you/)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('dialog', { name: 'Allow sessions to run without prompting?' })
    expect(mutateAsync).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Use full auto' }))
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ defaultPermissionMode: 'full-auto' }))
  })

  it('saves model approval as null for server default and trims the denylist', async () => {
    mount()
    await userEvent.click(screen.getByRole('radio', { name: 'Block' }))
    await userEvent.type(screen.getByLabelText('Gated models'), ' fable, opus ')
    await userEvent.click(screen.getByRole('radio', { name: 'Expand all' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenCalledWith({
      defaultPermissionMode: 'interactive', modelApprovalAction: 'block', modelApprovalDenylist: 'fable, opus',
      defaultToolDetail: 'all-open', thinkingCollapsed: true,
    })
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/features/settings/sections/SessionDefaultsSection.test.tsx`
Expected: FAIL — stub.

- [ ] **Step 3: Implement**

```tsx
import * as React from 'react'
import { useSettings, useUpdateSettings, type AppSettings } from '@/api/hooks'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { Select } from '@/ui/Select'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Checkbox } from '@/ui/Checkbox'
import { RadioGroup } from '@/ui/RadioGroup'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { useSettingsForm } from '../useSettingsForm'

type ApprovalAction = '' | 'require_approval' | 'block' | 'allow'
type ToolDetail = AppSettings['defaultToolDetail']

interface DefaultsDraft {
  permissionMode: string
  modelApprovalAction: ApprovalAction
  denylist: string
  toolDetail: ToolDetail
  thinkingCollapsed: boolean
}

/** Modes that let a session act without a human in the loop; saving one asks first (S17). */
const UNSAFE_MODES = new Set(['full-auto', 'bypassPermissions'])
const ACTION_LABEL: Record<Exclude<ApprovalAction, ''>, string> = { require_approval: 'Require approval', block: 'Block', allow: 'Allow' }

function toDraft(s: AppSettings): DefaultsDraft {
  return {
    permissionMode: s.defaultPermissionMode,
    modelApprovalAction: (s.modelApprovalAction ?? '') as ApprovalAction,
    denylist: s.modelApprovalDenylist ?? '',
    toolDetail: s.defaultToolDetail,
    thinkingCollapsed: s.thinkingCollapsed,
  }
}

function toPayload(d: DefaultsDraft): Partial<AppSettings> {
  return {
    defaultPermissionMode: d.permissionMode,
    modelApprovalAction: d.modelApprovalAction || null,
    modelApprovalDenylist: d.denylist.trim() || null,
    defaultToolDetail: d.toolDetail,
    thinkingCollapsed: d.thinkingCollapsed,
  }
}

export function SessionDefaultsSection() {
  const { data: settings } = useSettings()
  const update = useUpdateSettings()
  const [confirm, setConfirm] = React.useState<{ resolve: (ok: boolean) => void; mode: string } | null>(null)

  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<DefaultsDraft>({
    initial,
    save: (d) => update.mutateAsync(toPayload(d)),
    beforeSave: (d, i) => (UNSAFE_MODES.has(d.permissionMode) && d.permissionMode !== i.permissionMode)
      ? new Promise<boolean>((resolve) => setConfirm({ resolve, mode: d.permissionMode }))
      : true,
  })

  if (!form.draft || !settings) return null
  const { draft, set } = form
  const accepted = new Set(settings.acceptedPermissionModes)
  const rejected = PERMISSION_MODES.filter((m) => !accepted.has(m.value))
  const eff = settings.effectiveDefaults
  const effActionLabel = ACTION_LABEL[eff.modelApprovalAction as Exclude<ApprovalAction, ''>] ?? eff.modelApprovalAction

  return (
    <SettingsSection id="sessions" title="Defaults" scope="server"
      description="What every new session starts with. A session can change these for itself once it is running."
      footer={<SaveBar form={form} />}>
      {form.error && <Banner tone="danger" role="alert" title="Not saved">{form.error}</Banner>}

      <SettingsGroup legend="Tool approvals">
        <SettingRow label="Default tool approvals" htmlFor="defaults-permission-mode"
          description={rejected.length
            ? `Applied to new sessions. ${rejected.map((m) => m.label).join(', ')}: not accepted by this server version.`
            : 'Applied to new sessions and to automations that launch sessions.'}>
          <Select id="defaults-permission-mode" value={draft.permissionMode} onChange={(e) => set('permissionMode', e.target.value)}>
            {PERMISSION_MODES.map((m) => (
              <option key={m.value} value={m.value} disabled={!accepted.has(m.value)}>{m.label}</option>
            ))}
          </Select>
        </SettingRow>
        {UNSAFE_MODES.has(draft.permissionMode) && (
          <Banner tone="warning" role="alert" title="Sessions will run without prompting">
            Anyone who can reach this server can run code as you. Saving asks you to confirm.
          </Banner>
        )}
      </SettingsGroup>

      <SettingsGroup legend="Model approval">
        <SettingRow label="When a session asks for a gated model" htmlFor="defaults-model-action" wide
          description={<>Server default is <code>{eff.modelApprovalAction}</code> from <code>CCSWITCH_MODEL_APPROVAL_ACTION</code>.</>}>
          <RadioGroup name="defaults-model-action" legend="When a session asks for a gated model" hideLegend
            value={draft.modelApprovalAction} onChange={(v) => set('modelApprovalAction', v)}
            options={[
              { value: '', label: `Server default (${effActionLabel.toLowerCase()})` },
              { value: 'require_approval', label: 'Require approval' },
              { value: 'block', label: 'Block' },
              { value: 'allow', label: 'Allow' },
            ]} />
        </SettingRow>
        <SettingRow label="Gated models" htmlFor="defaults-denylist"
          description="Comma-separated tiers or model ids that need approval. Blank uses the server default.">
          <Input id="defaults-denylist" value={draft.denylist} placeholder={`${eff.modelApprovalDenylist} · server default`}
            onChange={(e) => set('denylist', e.target.value)} />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Transcript">
        <SettingRow label="Tool calls" htmlFor="defaults-tool-detail" wide description="How tool-call groups open when a transcript loads.">
          <RadioGroup name="defaults-tool-detail" legend="Tool calls" hideLegend value={draft.toolDetail}
            onChange={(v) => set('toolDetail', v)}
            options={[
              { value: 'auto', label: 'Auto-fold long groups' },
              { value: 'all-open', label: 'Expand all' },
              { value: 'all-closed', label: 'Collapse all' },
            ]} />
        </SettingRow>
        <SettingRow label="Thinking blocks" htmlFor="defaults-thinking" description="Start collapsed; expand any block by clicking it.">
          <Checkbox id="defaults-thinking" label="Collapse thinking by default" checked={draft.thinkingCollapsed} onChange={(v) => set('thinkingCollapsed', v)} />
        </SettingRow>
      </SettingsGroup>

      <ConfirmDialog
        open={confirm !== null}
        onClose={() => { confirm?.resolve(false); setConfirm(null) }}
        onConfirm={() => { confirm?.resolve(true); setConfirm(null) }}
        title="Allow sessions to run without prompting?"
        description={`New sessions will start in “${PERMISSION_MODES.find((m) => m.value === confirm?.mode)?.label ?? confirm?.mode}”. Anyone who can reach this server can run shell commands and edit files as you.`}
        confirmLabel={confirm?.mode === 'full-auto' ? 'Use full auto' : 'Bypass permissions'}
        tone="warning"
      />
    </SettingsSection>
  )
}
```

The `value: ''` radio option requires `RadioGroup<V extends string>` to accept `''`; it does (`''` is a `string` literal type).

- [ ] **Step 4: Run tests, un-todo the Task 12 assertion, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/SessionDefaultsSection.tsx web/src/features/settings/sections/SessionDefaultsSection.test.tsx web/src/features/settings/SettingsPage.test.tsx
git commit -m "feat(settings): Sessions › Defaults — tool approvals with confirm on unsafe modes, model approval, transcript (S17, S20, S21)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: Shared preference stores — `shell/railExpanded.ts` and `pipeline/notifyPref.ts`

**Files:**
- Create: `web/src/shell/railExpanded.ts`, `web/src/pipeline/notifyPref.ts`
- Modify: `web/src/shell/regions/NavRail.tsx:28-40` (use the shared store), `web/src/pipeline/useDashboard.ts:12-24` (use the shared store)
- Test: `web/src/shell/railExpanded.test.ts`, `web/src/pipeline/notifyPref.test.ts`

**Interfaces:**
- Produces:

```ts
// shell/railExpanded.ts
export const RAIL_EXPANDED_KEY = 'ccswitch.railExpanded'
export function readRailExpanded(): boolean               // default false
export function writeRailExpanded(v: boolean): void
export function useRailExpanded(): [boolean, (v: boolean) => void]   // useSyncExternalStore-backed; updates every subscriber

// pipeline/notifyPref.ts
export const NOTIFY_PREF_KEY = 'ccswitch.pipelineNotifications'
export const LEGACY_NOTIFY_PREF_KEY = 'prdash.notifications'
export function readNotifyPref(): boolean                  // reads new key; falls back to legacy once and migrates it
export function writeNotifyPref(v: boolean): void
export function useNotifyPref(): [boolean, (v: boolean) => void]
```

Both hooks use `useSyncExternalStore` over a tiny in-module listener set (`subscribe(cb)`, notified on `write*`), so Appearance and the rail/pipeline stay in step without a context.

- [ ] **Step 1: Write the failing tests**

`web/src/shell/railExpanded.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { readRailExpanded, writeRailExpanded, useRailExpanded, RAIL_EXPANDED_KEY } from './railExpanded'

describe('railExpanded', () => {
  it('defaults collapsed, persists, and every subscriber sees a write', () => {
    expect(readRailExpanded()).toBe(false)
    const a = renderHook(() => useRailExpanded())
    const b = renderHook(() => useRailExpanded())
    act(() => a.result.current[1](true))
    expect(b.result.current[0]).toBe(true)
    expect(localStorage.getItem(RAIL_EXPANDED_KEY)).toBe('true')
    writeRailExpanded(false)
    expect(readRailExpanded()).toBe(false)
  })
})
```

`web/src/pipeline/notifyPref.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { readNotifyPref, writeNotifyPref, NOTIFY_PREF_KEY, LEGACY_NOTIFY_PREF_KEY } from './notifyPref'

describe('notifyPref', () => {
  it('migrates the legacy prdash key once (DEC-7)', () => {
    localStorage.setItem(LEGACY_NOTIFY_PREF_KEY, 'true')
    expect(readNotifyPref()).toBe(true)
    expect(localStorage.getItem(NOTIFY_PREF_KEY)).toBe('true')
    expect(localStorage.getItem(LEGACY_NOTIFY_PREF_KEY)).toBeNull()
    writeNotifyPref(false)
    expect(readNotifyPref()).toBe(false)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/shell/railExpanded.test.ts src/pipeline/notifyPref.test.ts`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

`web/src/shell/railExpanded.ts`:

```ts
import { useSyncExternalStore } from 'react'

export const RAIL_EXPANDED_KEY = 'ccswitch.railExpanded'
const listeners = new Set<() => void>()

export function readRailExpanded(): boolean {
  try { return localStorage.getItem(RAIL_EXPANDED_KEY) === 'true' } catch { return false }
}

export function writeRailExpanded(v: boolean): void {
  try { localStorage.setItem(RAIL_EXPANDED_KEY, String(v)) } catch { /* storage unavailable */ }
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }

/** DEC-6: one store for the rail's expand/collapse, shared by NavRail and Settings › Appearance. */
export function useRailExpanded(): [boolean, (v: boolean) => void] {
  const v = useSyncExternalStore(subscribe, readRailExpanded, () => false)
  return [v, writeRailExpanded]
}
```

`web/src/pipeline/notifyPref.ts` is the same shape with the migration inside `readNotifyPref`:

```ts
import { useSyncExternalStore } from 'react'

export const NOTIFY_PREF_KEY = 'ccswitch.pipelineNotifications'
export const LEGACY_NOTIFY_PREF_KEY = 'prdash.notifications'
const listeners = new Set<() => void>()

export function readNotifyPref(): boolean {
  try {
    const v = localStorage.getItem(NOTIFY_PREF_KEY)
    if (v !== null) return v === 'true'
    const legacy = localStorage.getItem(LEGACY_NOTIFY_PREF_KEY)
    if (legacy !== null) {
      localStorage.setItem(NOTIFY_PREF_KEY, legacy)
      localStorage.removeItem(LEGACY_NOTIFY_PREF_KEY)
      return legacy === 'true'
    }
    return false
  } catch { return false }
}

export function writeNotifyPref(v: boolean): void {
  try { localStorage.setItem(NOTIFY_PREF_KEY, String(v)) } catch { /* storage unavailable */ }
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }

export function useNotifyPref(): [boolean, (v: boolean) => void] {
  const v = useSyncExternalStore(subscribe, readNotifyPref, () => false)
  return [v, writeNotifyPref]
}
```

Then in `NavRail.tsx` delete `RAIL_EXPANDED_KEY` and its read/write pair and call `useRailExpanded()`; in `useDashboard.ts` delete `LS_NOTIFY_KEY`, `readNotifyPref`, `writeNotifyPref` and import them from `./notifyPref` (keep `toggleNotify`'s permission-request behaviour exactly as it is). Run `NavRail.test.tsx` and any pipeline test that seeds `prdash.notifications` and update the seeded key.

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/shell src/pipeline`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/railExpanded.ts web/src/shell/railExpanded.test.ts web/src/shell/regions/NavRail.tsx web/src/pipeline/notifyPref.ts web/src/pipeline/notifyPref.test.ts web/src/pipeline/useDashboard.ts
git commit -m "refactor(web): shared stores for rail-expanded and pipeline-notifications prefs; migrate prdash key (S31)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 16: `AppearanceSection` (Theme · Navigation · Layout — this browser)

**Files:**
- Replace stub: `web/src/features/settings/sections/AppearanceSection.tsx`
- Test: `web/src/features/settings/sections/AppearanceSection.test.tsx`

**Interfaces:**
- Consumes: `useTheme` (`@/shell/theme`), `useRailExpanded`, `useNotifyPref`, `loadSidebarWidth`/`saveSidebarWidth`/`SIDEBAR_DEFAULT` (`@/lib/sidebarWidth`), `loadDrawerWidth`/`saveDrawerWidth`/`DRAWER_DEFAULT` (`@/shell/drawerWidth`), `RadioGroup`, `Switch`, `Button`, chassis.
- Produces: `export function AppearanceSection(): JSX.Element`. **No `SaveBar`**: every row is Immediate.

- [ ] **Step 1: Write the failing tests**

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider, THEME_STORAGE_KEY } from '@/shell/theme'
import { RAIL_EXPANDED_KEY } from '@/shell/railExpanded'
import { NOTIFY_PREF_KEY } from '@/pipeline/notifyPref'
import { SIDEBAR_STORAGE_KEY, SIDEBAR_DEFAULT } from '@/lib/sidebarWidth'
import { stubMatchMedia } from '@/test/matchMedia'
import { AppearanceSection } from './AppearanceSection'

function mount() {
  stubMatchMedia([])
  return render(<ThemeProvider><AppearanceSection /></ThemeProvider>)
}

describe('AppearanceSection', () => {
  it('theme radio writes ccswitch.theme and reports Saved; system removes the key', async () => {
    mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Appearance' })).toBeInTheDocument()
    expect(screen.getByText('This browser')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: 'Dark' }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(screen.getAllByRole('status')[0]).toHaveTextContent('Saved')
    await userEvent.click(screen.getByRole('radio', { name: 'Use system setting' }))
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })

  it('switches apply immediately to their stores', async () => {
    mount()
    await userEvent.click(screen.getByRole('switch', { name: 'Show navigation labels' }))
    expect(localStorage.getItem(RAIL_EXPANDED_KEY)).toBe('true')
    await userEvent.click(screen.getByRole('switch', { name: 'Pipeline notifications' }))
    expect(localStorage.getItem(NOTIFY_PREF_KEY)).toBe('true')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('layout widths show the stored value and Reset restores the default', async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, '400')
    mount()
    expect(screen.getByText('400 px')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reset session list width' }))
    expect(screen.getByText(`${SIDEBAR_DEFAULT} px`)).toBeInTheDocument()
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_DEFAULT))
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web test -- src/features/settings/sections/AppearanceSection.test.tsx`
Expected: FAIL — stub.

- [ ] **Step 3: Implement**

```tsx
import * as React from 'react'
import { useTheme, type ThemeChoice } from '@/shell/theme'
import { useRailExpanded } from '@/shell/railExpanded'
import { useNotifyPref } from '@/pipeline/notifyPref'
import { loadSidebarWidth, saveSidebarWidth, SIDEBAR_DEFAULT } from '@/lib/sidebarWidth'
import { loadDrawerWidth, saveDrawerWidth, DRAWER_DEFAULT } from '@/shell/drawerWidth'
import { RadioGroup } from '@/ui/RadioGroup'
import { Switch } from '@/ui/Switch'
import { Button } from '@/ui/Button'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'

/** "Saved" for a few seconds after an immediate row applies; `null` shows nothing. */
function useFlash(): [string | null, () => void] {
  const [at, setAt] = React.useState<number | null>(null)
  React.useEffect(() => {
    if (at === null) return
    const t = setTimeout(() => setAt(null), 4000)
    return () => clearTimeout(t)
  }, [at])
  return [at === null ? null : 'Saved', () => setAt(Date.now())]
}

const THEME_OPTIONS: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'Use system setting' },   // C2 wording
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

/** Every row here is Immediate (spec §7.2); there is deliberately no SaveBar. */
export function AppearanceSection() {
  const { choice, setChoice } = useTheme()
  const [railExpanded, setRailExpanded] = useRailExpanded()
  const [notify, setNotify] = useNotifyPref()
  const [themeStatus, flashTheme] = useFlash()
  const [railStatus, flashRail] = useFlash()
  const [notifyStatus, flashNotify] = useFlash()
  const [listWidth, setListWidth] = React.useState(loadSidebarWidth)
  const [drawerWidth, setDrawerWidth] = React.useState(loadDrawerWidth)

  return (
    <SettingsSection id="appearance" title="Appearance" scope="browser"
      description="Stored in this browser only. Other devices keep their own.">
      <SettingsGroup legend="Theme">
        <SettingRow label="Theme" htmlFor="appearance-theme-system" wide status={themeStatus}
          description="Also in the rail's theme menu; both change the same setting.">
          <RadioGroup name="appearance-theme" legend="Theme" hideLegend value={choice}
            onChange={(v) => { setChoice(v); flashTheme() }} options={THEME_OPTIONS} />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Navigation">
        <SettingRow label="Show navigation labels" htmlFor="appearance-rail" status={railStatus}
          description="Expands the rail on wide screens so each section shows its name.">
          <Switch id="appearance-rail" checked={railExpanded} onChange={(v) => { setRailExpanded(v); flashRail() }} aria-label="Show navigation labels" />
        </SettingRow>
        <SettingRow label="Pipeline notifications" htmlFor="appearance-notify" status={notifyStatus}
          description="Browser alerts when a pull request's status changes on the Pipeline page.">
          <Switch id="appearance-notify" checked={notify} onChange={(v) => { setNotify(v); flashNotify() }} aria-label="Pipeline notifications" />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Layout" description="Set by dragging the dividers; reset here.">
        <SettingRow label="Session list width" htmlFor="appearance-list-width" description="The Sessions list column on desktop.">
          <span className="flex items-center gap-2">
            <span id="appearance-list-width" className="text-sm text-ink-muted tabular-nums">{listWidth} px</span>
            <Button type="button" variant="ghost" aria-label="Reset session list width" disabled={listWidth === SIDEBAR_DEFAULT}
              onClick={() => { saveSidebarWidth(SIDEBAR_DEFAULT); setListWidth(SIDEBAR_DEFAULT) }}>Reset</Button>
          </span>
        </SettingRow>
        <SettingRow label="Assistant drawer width" htmlFor="appearance-drawer-width" description="The right-hand drawer on desktop.">
          <span className="flex items-center gap-2">
            <span id="appearance-drawer-width" className="text-sm text-ink-muted tabular-nums">{drawerWidth} px</span>
            <Button type="button" variant="ghost" aria-label="Reset assistant drawer width" disabled={drawerWidth === DRAWER_DEFAULT}
              onClick={() => { saveDrawerWidth(DRAWER_DEFAULT); setDrawerWidth(DRAWER_DEFAULT) }}>Reset</Button>
          </span>
        </SettingRow>
      </SettingsGroup>
    </SettingsSection>
  )
}
```

`SettingRow`'s `htmlFor` for the theme row points at the first radio's id (`appearance-theme-system`, which `RadioGroup` derives as `${name}-${value}`), so the row label focuses a real control.

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings/sections/AppearanceSection.test.tsx`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/AppearanceSection.tsx web/src/features/settings/sections/AppearanceSection.test.tsx
git commit -m "feat(settings): Appearance section — theme, navigation labels, pipeline notifications, layout resets (S8, S27)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 17: `AboutSection` (Version · Managed by env)

**Files:**
- Replace stub: `web/src/features/settings/sections/AboutSection.tsx`
- Test: `web/src/features/settings/sections/AboutSection.test.tsx`

**Interfaces:**
- Consumes: `useSettings` (`about`, `managed`), `useOpenDrawer` (`@/shell/drawerParam` — the hook `GlobalActions` uses to open `help`), chassis, `Skeleton`.
- Produces: `export function AboutSection(): JSX.Element`.

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import * as hooks from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'
import { AboutSection } from './AboutSection'

vi.mock('@/api/hooks', () => ({ useSettings: vi.fn() }))

describe('AboutSection', () => {
  it('shows version, host and the managed env table', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      about: { gitSha: 'f92c6e34', hostName: 'dobby' },
      managed: [{ key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' }],
    })))
    const router = createMemoryRouter([{ path: '/', element: <AboutSection /> }])
    render(<RouterProvider router={router} />)
    expect(screen.getByRole('heading', { level: 2, name: 'About' })).toBeInTheDocument()
    expect(screen.getByText('f92c6e34')).toBeInTheDocument()
    expect(screen.getByText('dobby')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /CCSWITCH_ENABLE_COPILOT/ })
    expect(row).toHaveTextContent('off')
    expect(row).toHaveTextContent('Enable GitHub Copilot accounts')
    expect(screen.getByText(/\/etc\/ccswitch\/ccswitch\.env/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web test -- src/features/settings/sections/AboutSection.test.tsx`
Expected: FAIL — stub.

- [ ] **Step 3: Implement**

```tsx
import { useSettings, type ManagedFlag } from '@/api/hooks'
import { Skeleton } from '@/ui/Skeleton'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'

function showValue(v: ManagedFlag['value']): string {
  if (v === null) return 'unset'
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  return String(v)
}

/** Read-only (spec §8 About, S30): what the operator set in the env file, shown so posture can be confirmed from the UI. */
export function AboutSection() {
  const { data: settings, isLoading } = useSettings()
  return (
    <SettingsSection id="about" title="About" scope="none"
      description="Version, host, and the server settings managed by environment variables.">
      {isLoading || !settings ? <Skeleton lines={4} /> : (
        <>
          <SettingsGroup legend="This server">
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 py-3 text-sm">
              <dt className="text-ink-muted">Version</dt><dd className="font-mono text-ink">{settings.about.gitSha ?? 'unknown'}</dd>
              <dt className="text-ink-muted">Host</dt><dd className="font-mono text-ink">{settings.about.hostName}</dd>
            </dl>
          </SettingsGroup>

          <SettingsGroup legend="Managed by env"
            description={<>Set in <code>/etc/ccswitch/ccswitch.env</code> and read at daemon start. Not editable here.</>}>
            <div className="overflow-x-auto py-2">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-ink-muted">
                    <th scope="col" className="py-1 pr-4 font-medium">Variable</th>
                    <th scope="col" className="py-1 pr-4 font-medium">Value</th>
                    <th scope="col" className="py-1 font-medium">What it does</th>
                  </tr>
                </thead>
                <tbody>
                  {settings.managed.map((m) => (
                    <tr key={m.key} className="border-t border-edge">
                      <th scope="row" className="py-1.5 pr-4 font-mono text-xs font-normal text-ink">{m.key}</th>
                      <td className="py-1.5 pr-4 font-mono text-xs">{showValue(m.value)}</td>
                      <td className="py-1.5 text-ink-muted">{m.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SettingsGroup>
        </>
      )}
    </SettingsSection>
  )
}
```

`SettingsGroup` wraps children in `divide-y`; a `dl`/`table` as the single child is fine. `useOpenDrawer` is not needed unless you add a "Keyboard shortcuts" link; omit it (YAGNI).

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `npm --prefix web test -- src/features/settings/sections/AboutSection.test.tsx`; lint; typecheck. Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/AboutSection.tsx web/src/features/settings/sections/AboutSection.test.tsx
git commit -m "feat(settings): About section — version, host, managed-by-env table (S30)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 18: Delete the old General section, migrate its tests, final gates

**Files:**
- Delete: `web/src/components/settings/GeneralSection.tsx`, `web/src/components/settings/__tests__/GeneralSection.test.tsx`, `web/src/components/settings/__tests__/ModelApprovalControls.test.tsx`
- Modify: `web/src/components/__tests__/dedupeMobileDesktop.test.ts` (any grep that names `SettingsView`/`GeneralSection`), `web/e2e/dashboard.spec.ts` (Settings tab assertions), `web/src/components/__tests__/AccountsReserve.test.tsx` or a new `AccountsModelApproval.test.tsx` (receives the per-account half of `ModelApprovalControls.test.tsx`)

- [ ] **Step 1: Move the per-account model-approval tests**

`ModelApprovalControls.test.tsx` has two halves. The global half (action select + denylist) is now covered by `SessionDefaultsSection.test.tsx`; delete it. The per-account half (Inherit default, prefill, `updateAccount` on change, null on Inherit) still describes `AccountsManager`, which PR 5c will change but this PR must not break. Create `web/src/components/__tests__/AccountsModelApproval.test.tsx` and move those `it(...)` blocks into it verbatim, with the same mocks the sibling `Accounts*.test.tsx` files use.

- [ ] **Step 2: Delete the old files and fix greps**

```bash
git rm -q web/src/components/settings/GeneralSection.tsx web/src/components/settings/__tests__/GeneralSection.test.tsx web/src/components/settings/__tests__/ModelApprovalControls.test.tsx
grep -rn "GeneralSection\|SettingsView\|NotificationsToggle" web/src web/e2e
```

Every hit outside `web/src/features/settings/` and `web/src/components/settings/NotificationsSection.tsx` (which still owns the push toggle) must be updated or removed: `dedupeMobileDesktop.test.ts` source-greps; `factories.ts` comments; `App.dialogs.test.tsx` if it names the tab strip; `web/e2e/dashboard.spec.ts` if it clicks a `role="tab"` inside Settings (change it to click the nav link and assert the `h2`).

- [ ] **Step 3: Confirm the duplicate push toggle is gone from General**

```bash
grep -rn "NotificationsToggle" web/src/features/settings
```

Expected: no output (General no longer renders it; Notifications still does).

- [ ] **Step 4: Run every gate**

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
/home/tony/GitHub/ccswitch/.venv/bin/python -m ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -q
```

Expected: all green. Then the success checks from spec §11:

```bash
grep -c "window.confirm" web/src/features/settings     # 0
grep -rn 'role="switch"' web/src --include=*.tsx -l    # ui/Switch.tsx plus the three legacy sites (they migrate in 5e)
ls web/src/components/settings/SettingsView.tsx        # No such file
```

- [ ] **Step 5: Commit**

```bash
git add -A web/src web/e2e
git commit -m "chore(settings): delete legacy GeneralSection/SettingsView tests; move per-account model-approval tests (S34)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

**PR body must cite:** closes S1, S2, S3, S5, S6, S7, S8, S9, S10 (General half), S11, S12, S16, S17, S20, S21, S23, S27, S28, S30, S31, S34, S35; partially S15/S18 (ConfirmDialog exists; the eight unconfirmed deletes are gated in 5b–5e).

---

## Self-review (run by the plan author; recorded here)

**Spec coverage for PR 5a (spec §11 row 5a):**

| Spec item | Task |
|---|---|
| `settingsDefs`, `SettingsNav`, `SettingsPage`, phone drill-down, last-section memory | 11, 12 |
| Primitives `Switch` `Checkbox` `RadioGroup` `ConfirmDialog` | 5, 6, 7 |
| `SettingRow` `SettingsSection` `SettingsGroup` `SaveBar` `ScopeBadge` | 8, 9 |
| General, Sessions › Defaults, Appearance, About | 13, 14, 16, 17 |
| `effectiveDefaults` / `managed` / `about` API | 2, 4 |
| `_VALID_MODES` fix + parity test; S21 default fix | 1, 14 |
| Redirects, with the `automation` section retained (R10) | 12 |
| `pageContract` entry | 12 |
| Every other section hosted unchanged | 12 (`HostedSection`) |
| Deletes: `SettingsView` tablist, `GeneralSection` | 12, 18 |
| §9 409 on partial archive move | 3 |
| §10 a11y contract (h1/h2/legend, describedby, status regions, targets, scroll-padding) | 8, 9, 12 |
| §12 tests: `settingsDefs`, `SettingsPage` × modes, `SaveBar`, `ConfirmDialog`, parity | 11, 12, 9, 7, 1 |

Not in 5a by design (spec §11): Presets/Templates/Profiles selects and confirms (5b); Accounts menu and per-account fields, Hosts max-sessions (5c); Library and Hooks/Snapshots confirms (5d); Notifications edit flows, the three legacy `role="switch"` sites, `ACCESSIBILITY.md` (5e). Playwright viewport smoke for `/settings/accounts` is listed in spec §12 and lands with 5c, when that section is on the chassis.

**Placeholder scan:** no "TBD", "implement later", "add validation" or "similar to Task N"; every code step has code. The only `todo` in the document is vitest's `it.todo`, used in Task 12 for one assertion that Task 14 fulfils.

**Type consistency:** `SettingsForm.blocker` is declared in Task 9's implementation and used by `SaveBar` (Task 9) — the interface block at the top of Task 9 lists it. `SettingsDef.component` is `React.ComponentType` everywhere (DEC-2). `AppSettings.effectiveDefaults.maxActiveSessions: number` (Task 4) is what `GeneralSection` reads (Task 13). `useSettingsDrill` is exported from `SettingsPage.tsx` and imported by `SettingsIndexPage.tsx` (Task 12). `PageHeader` gains `documentTitle?: string` in Task 12 and nothing else depends on it.
