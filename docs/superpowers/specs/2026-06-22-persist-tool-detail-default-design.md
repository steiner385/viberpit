# Persist tool-fold as a Settings default

**Date:** 2026-06-22
**Status:** Approved (design)

## Problem

The session transcript's tool-call fold density (`toolDetailMode`: `auto` /
`all-closed` / `all-open`) is ephemeral per-session React state in
`useSessionRuntime` (`useState('auto')`). It is surfaced by a "Collapse tools"
button in the session top bar plus a `c`/`C` keyboard shortcut. Two problems:

1. **No persistence.** The choice resets to `auto` on every session
   switch/reopen because the runtime remounts (desktop provider is keyed by
   session id; mobile `SessionView` unmounts on Back).
2. **Scattered control.** The toggle is a labeled button in the header bar,
   separate from the app's actual settings home (the gear-icon `SettingsPanel`),
   which already persists `defaultPermissionMode`, `thinkingCollapsed`, and
   `repoRoot` to a server-backed `AppSettings` singleton.

Related finding: `thinkingCollapsed` is **half-wired** — `SettingsPanel` saves
it and the backend stores it, but `ThinkingBlock.tsx` hardcodes `useState(false)`
and never reads it, so the setting currently does nothing.

## Goal

Make tool-fold a **persisted global default** living in `AppSettings`, with the
Settings panel as the *single* control. Remove the scattered in-session toggle.
While consolidating, actually wire up the dead `thinkingCollapsed` setting and
group both under one "Transcript display" section — mirroring Claude Code's
model of a persistent config default rather than scattered header buttons.

## Non-goals

- No new persistence infrastructure — reuse the existing `/api/settings`
  singleton + React Query `useSettings`/`useUpdateSettings`.
- No per-session remembered override and no transient keyboard toggle. The
  persisted default is the only global control. (Per-group expand/collapse via
  clicking a `ToolGroup` header is unchanged and remains the in-session
  granular affordance.)
- `ReviewView` keeps its literal `toolDetailMode="auto"` (read-only diff view).

## Behavior

- `toolDetailMode` is derived live from `AppSettings.defaultToolDetail`. Changing
  it in Settings re-renders every mounted session (React Query invalidation on
  save), so all open sessions update together.
- `thinkingCollapsed` controls `ThinkingBlock`'s initial open state
  (`open = !thinkingCollapsed`), with a per-instance manual-toggle override so a
  user's click sticks regardless of the setting.
- Per-`ToolGroup` header clicks (`userExpanded`) still expand/collapse an
  individual group in-session — unaffected.

## Backend (`server/`)

- `db/models.py`: add `default_tool_detail` to `AppSettings` as a **nullable**
  `String` (`Mapped[str | None]`, `nullable=True`). Nullable is load-bearing:
  `create_all` never `ALTER`s an existing table, so on the live SQLite deploy a
  NOT-NULL column would be absent and every read would break; a nullable column
  is backfilled by `init_db`, and reads apply a `'auto'` fallback.
- `api/settings.py`:
  - `SettingsBody`: add `defaultToolDetail: str | None = None`.
  - Add `_VALID_TOOL_DETAIL = {"auto", "all-closed", "all-open"}`; in PUT,
    accept the value only when present and valid (mirrors `_VALID_MODES`).
  - `_to_dict`: expose `"defaultToolDetail": s.default_tool_detail or "auto"`.

## Frontend (`web/`)

- `api/hooks.ts`: `AppSettings` gains `defaultToolDetail: ToolDetailMode`.
- `session/useSessionRuntime.ts`: remove the `toolDetailMode` `useState` and
  `toggleToolDetail`. Read `useSettings()` and expose:
  - `toolDetailMode = settings?.defaultToolDetail ?? 'auto'`
  - `thinkingCollapsed = settings?.thinkingCollapsed ?? true`
  Remove `toggleToolDetail` from the `SessionRuntime` interface.
- `components/session/SessionView.tsx`: remove the top-bar "Collapse tools"
  button and its `toolToggleText`/`toolToggleAria` label logic; remove the
  `c`/`C` keydown branch and update the shortcut comment.
- `components/DesktopShell.tsx`: remove the mirrored "Collapse tools" button and
  its duplicated label expression.
- `components/ShortcutsHelp.tsx`: remove the `c` (toggle tool detail) entry.
- `components/session/ThinkingBlock.tsx`: accept `defaultCollapsed: boolean`.
  Initial open derives from it; a per-instance manual override sticks:
  ```ts
  const [userToggled, setUserToggled] = useState<boolean | null>(null)
  const open = userToggled ?? !defaultCollapsed
  ```
  Thread `thinkingCollapsed` from the runtime through `SessionBody` →
  `TranscriptView` → `Turn` → `ThinkingBlock` (same path `toolDetailMode` uses).
- `components/settings/SettingsPanel.tsx`: add a **"Transcript display"** group:
  - *Tool calls default* `<select>`: Auto-fold (`auto`) / Expand all
    (`all-open`) / Collapse all (`all-closed`).
  - Relocate the existing *Collapse thinking by default* checkbox under it.
  - `handleSave` includes `defaultToolDetail`.

## Tests

- Server `tests/test_settings.py`: GET default is `auto`; PUT round-trips a valid
  value; PUT rejects/ignores an invalid value.
- Web:
  - Rework `SessionView.toolDetail.test.tsx`: mode comes from settings; no
    toggle button or `c` shortcut.
  - Update `DesktopShell.test.tsx` (button removed).
  - `SettingsPanel.test.tsx`: the tool-detail select saves `defaultToolDetail`.
  - `ThinkingBlock` test: initial open honors `defaultCollapsed`; manual toggle
    overrides.

## Risks

- Live-DB column backfill depends on `init_db` adding the nullable column —
  verify the backfill path during implementation.
- Removing the `c` shortcut is a small behavior loss for keyboard users;
  accepted in favor of a single source of truth.
