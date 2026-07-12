# Library UI — managing plugins, skills, slash commands, and MCP servers

**Date:** 2026-07-12
**Status:** approved for implementation (autonomous session; user requested "design and build")

## Problem

ccswitch manages multiple Claude accounts and their sessions, but the extension surface that
shapes every session — installed plugins, user skills, slash commands, and MCP servers — is
invisible and unmanageable from the web UI. Today these are edited by hand-editing files under
`~/.claude` and `~/.claude-accounts/store/`, which is error-prone and impossible from a phone.

## Key facts (verified against the codebase and live config dirs)

- `plugins/`, `commands/`, `settings.json` are **shared across accounts** via the symlink
  allowlist (`src/ccswitch/configdir.py:15`). Skills live in the shared base too
  (`~/.claude/skills/`). Managing them is one operation for all accounts.
- Plugin enablement is `settings.json → enabledPlugins: {"<id>@<marketplace>": bool}` in the
  shared base. Inventory lives in `plugins/installed_plugins.json`; rich metadata (description,
  category, per-model token cost, component lists) in `plugins/plugin-catalog-cache.json`;
  marketplaces in `plugins/known_marketplaces.json`.
- User-scope MCP servers are the `mcpServers` key of `store/claude.shared.json`.
  `compose_claude_json` (`src/ccswitch/configdir.py:90`) overlays that key **wholesale** into
  each account's `.claude.json` at session launch, so the shared file is the single write
  target and edits/deletes stick. The reverse harvest (`_merge_shared_claude_json`) runs only
  during account login capture.
- Per-account MCP auth state is `cfg/<account>/mcp-needs-auth-cache.json`.
- No existing server API or web UI touches any of this — greenfield, no parallel-implementation
  risk (grep verified across `server/`, `src/`, `web/src/`).

## Scope

### In (v1)

A new top-level **Library** tab with four sections:

1. **Plugins** — list installed plugins with enabled toggle, version, description, category,
   token cost, and component summary (commands/skills/agents/MCP/hooks). Detail view enumerates
   components by shallow-scanning the plugin's `installPath`. Marketplaces listed read-only.
   Mutation: enable/disable only (read-modify-write of `enabledPlugins` in shared
   `settings.json`).
2. **Skills** — user skills (`<shared>/skills/<name>/SKILL.md`): list with name/description
   from frontmatter, view/edit raw markdown, create, delete. Plugin-provided skills listed
   read-only with a source-plugin badge (only from enabled plugins).
3. **Commands** — same shape over `<shared>/commands/**/*.md` (nested paths render as
   namespaced names, e.g. `project/gh-fix` → `/project:gh-fix`). Plugin commands read-only.
4. **MCP servers** — user-scope servers from `store/claude.shared.json → mcpServers`: list,
   create, edit, delete via a typed form (stdio: command/args/env; http/sse: url/headers) with
   a raw-JSON escape hatch. Plugin-provided servers read-only. Each server shows per-account
   "needs auth" badges from `mcp-needs-auth-cache.json`. UI notes that changes apply to newly
   started sessions.

### Out (deliberately)

- **Plugin install/uninstall/update.** Installation is the Claude binary's git-clone + cache +
  registry machinery; replicating it in ccswitch violates the spirit of constitution I/III
  (never fork/patch; verify against the real binary). Future work may shell out to the
  `claude plugin` CLI once its behavior is verified.
- **Per-project MCP config** (`projects[path].mcpServers` / `enabledMcpjsonServers`) — a later
  iteration; v1 handles user scope, which is what ccswitch shares.
- Hooks and agents management (surfaced read-only inside plugin details only).
- Editing plugin-owned files — always read-only.

## Architecture

### Backend

- **`server/library/` package** — all domain logic, pure functions over the filesystem:
  - `scan.py`: `list_plugins()`, `plugin_detail(id)`, `list_skills()`, `read_skill(name)`,
    `list_commands()`, `read_command(path)`, `list_mcp_servers()`. Reads shared base via
    `ccswitch.paths.shared_base()` and store via `ccswitch.paths`; parses frontmatter with
    PyYAML (already a dependency). Never walks `plugins/cache/` wholesale (28 GB) — only the
    shallow component dirs of installed plugins' `installPath`s, and prefers catalog-cache
    metadata where present.
  - `mutate.py`: `set_plugin_enabled(id, bool)`, `write_skill(name, content)`,
    `delete_skill(name)`, `write_command(path, content)`, `delete_command(path)`,
    `put_mcp_server(name, config)`, `delete_mcp_server(name)`. JSON mutations are
    read-modify-write through `ccswitch.creds.write_json_secure` (atomic tempfile+rename);
    markdown writes use the same tempfile+rename pattern.
  - `names.py`: strict identifier validation — skills `[a-z0-9][a-z0-9-]*`; command paths
    limited to slug segments joined by `/`; MCP names `[A-Za-z0-9._-]+`; plugin ids
    `<slug>@<slug>`. Every resolved path must pass a `resolve().is_relative_to(base)`
    containment check (path-traversal defense).
- **`server/api/library.py`** — thin FastAPI router, prefix `/api/library`, all routes behind
  `Depends(current_identity)`; mutations call `record_audit`. Registered in `server/app.py`.

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/library/plugins` | — | merged inventory + enabled + catalog metadata + marketplaces |
| GET | `/api/library/plugins/{plugin_id}` | — | detail incl. component listing |
| PATCH | `/api/library/plugins/{plugin_id}` | `{enabled: bool}` | 404 if unknown |
| GET | `/api/library/skills` | — | user + plugin skills |
| GET/PUT/DELETE | `/api/library/skills/{name}` | PUT: `{content}` | user scope only; PUT creates or updates |
| GET | `/api/library/commands` | — | user + plugin commands |
| GET/PUT/DELETE | `/api/library/commands/{path:path}` | PUT: `{content}` | nested paths validated |
| GET | `/api/library/mcp` | — | user + plugin servers + per-account auth flags |
| PUT/DELETE | `/api/library/mcp/{name}` | PUT: `{config}` | config validated by type |

Direct file IO in the request process (edge or in-process) is correct here: no orchestrator
state is involved, so no daemon RPC/`RemoteOrchestrator` proxy is needed; both processes run as
the same user on the same host and all writes are atomic. The residual read-modify-write race
against a live `claude` process writing `settings.json` is accepted — it is the same window the
CLI itself has, and the window is microseconds around an atomic rename.

### Frontend

- New tab `library` ("Library") in `web/src/components/tabDefs.ts`, route `/library` in
  `App.tsx`, main-pane branch in `DesktopShell.tsx`, panel in `Dashboard.tsx` (with the
  `flex-1 min-h-0 overflow-y-auto` scroll wrapper the mobile tab panels require).
- `web/src/components/library/`:
  - `LibraryView.tsx` — section switcher (Plugins / Skills / Commands / MCP) driven by a
    `?section=` search param for deep linking; renders one section list at a time.
  - `PluginList.tsx` / `PluginRow` — toggle switch, badges (version, category, token cost),
    expandable detail with component breakdown.
  - `SkillList.tsx`, `CommandList.tsx` — rows with source badge (User / plugin name); user
    items open `MarkdownEditorDialog` (monospace textarea, save/delete, create-new flow);
    plugin items open it read-only.
  - `McpServerList.tsx` + `McpServerDialog` — typed form (type selector stdio/http/sse) with
    raw-JSON mode toggle; per-account needs-auth badges; "applies to new sessions" note.
- Data layer: `QK.library.plugins/skills/commands/mcp` keys and query/mutation hooks in
  `web/src/api/hooks.ts` following existing conventions (invalidate section key on mutation);
  types in `web/src/api/types.ts`.

### Error handling

- Missing shared base / store files → empty lists, never 500.
- Invalid names/paths → 400 with structured `{error: "invalid_name", ...}` detail (matches
  accounts.py convention). Unknown ids → 404. Editing plugin-owned items → 403.
- Malformed frontmatter/JSON in existing files → item still listed with a `parseError` flag
  rather than breaking the whole list.

### Testing

- `server/tests/test_library_api.py` (+ unit tests for scan/mutate/names): seed a fake shared
  base + store under the conftest-redirected `CCSWITCH_HOME` temp dir; cover list merging,
  enable toggle persistence, skill/command CRUD round-trips, MCP CRUD against
  `claude.shared.json`, traversal rejection (`../`, absolute, symlink escape), plugin-owned
  403, malformed-file resilience, audit records.
- Web: vitest for `LibraryView` section switching and each list's render/mutation wiring using
  the established `@/api/hooks` `vi.mock` pattern; `tsc --noEmit` + eslint green.

## Alternatives considered

1. **Per-account management UI** (pick an account, edit its config) — rejected: contradicts
   ccswitch's own share model; plugins/commands/settings are symlinked shared, so per-account
   editing is a fiction that would immediately desync.
2. **Settings-panel subsection instead of a top-level tab** — rejected: four entity types with
   CRUD and detail views outgrow a modal; the dashboard tab model is the established pattern
   for exactly this shape (Accounts, Automation are peers).
3. **Daemon-RPC-mediated file access** — rejected for v1: no orchestrator state involved,
   direct atomic IO is simpler and the memory-documented proxy burden applies only to
   orchestrator methods.
