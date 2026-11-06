# D-16 "Plugins, MCP per-tool toggles, output styles, app slash commands": verification spike

Date: 2026-11-04. Constitution III: every claim below was checked in one of three ways: against the installed binary,
by a no-model probe, or in ViberPit's code. Claims that were not checked are listed under **Gaps**.
Parent spec: `2026-11-01-ui-desktop-parity-design.md` lines 365-368, 403 and 432 (decision `[A-D15]`: "plugin
marketplace browse/install; MCP per-tool toggles (per server and per session); output styles; app-handled `/plan`
(and the other app slash commands) drive the same actions as the UI").
Earlier spikes: `2026-11-03-d9-rewind-verification.md`, `2026-11-04-d13-memory-verification.md`,
`2026-11-04-d14-session-settings-verification.md` (format and harness).

## 0. Versions and how to read the offsets

- **Binary (dobby).** `~/.local/bin/claude` resolves to `~/.local/share/claude/versions/2.1.318` (`claude --version`:
  `2.1.318 (Claude Code)`), sha256 prefix `708c77b204db3008`, the same file D-14 used. Every byte offset (`@N`) below
  is into this file. The bundle is minified, so each offset was read in context.
- **Binary (rocky).** Live worker sessions run `versions/2.1.293`. Its `plugin install --help` has `--json`,
  `plugin list --help` has `--available`, and its strings include `reload_output_styles` (9), `available_output_styles`
  (7), `mcp_toggle` (19), `reload_plugins` (20) and `apply_flag_settings` (40). No rocky probe was run.
- **Python SDK.** `claude-agent-sdk` 0.2.105 is in the deploy venv (`/home/tony/GitHub/viberpit-deploy/.venv`).
- **Probes.**
  - **CLI subcommand probes** (`claude plugin …`, `claude mcp …`) ran under `env -i` with a throwaway `HOME` and
    `CLAUDE_CONFIG_DIR` in `/tmp` and `ANTHROPIC_BASE_URL=http://127.0.0.1:9`. The marketplace was a local
    `directory` marketplace (`/tmp/d16-mkt`) holding one plugin, `hello` (one command, one output style). The only
    network use was the CLI's own fetch of the Anthropic plugin directory (§1.4).
  - **Control-request probes** use the D-9/D-14 harness (`test_settings_cli_contract._Cli`): a stream-json CLI, an env
    built from scratch, a throwaway config dir, control requests only. No user message was written, no `assistant`
    frame appeared and no model call was made. The MCP server was a 20-line stdio JSON-RPC stub with three tools
    (`tool_a`, `tool_b`, `tool_c`).
  - Scripts are in `/tmp/d16-scripts/` (`probe.py`, `mcp_probe.py`, `run1.py`…`run6.py`). Every probe config dir was
    removed afterwards. No real account dir under `~/.claude-accounts/` was written. Two were read: the shared
    `installed_plugins.json`/`settings.json`, for §1.3.

## 1. Plugins and marketplaces

### 1.1 CLI surface (`claude plugin --help`, 2.1.318)

| Command | Flags | Output |
|---|---|---|
| `plugin marketplace add <source>` | `--scope user\|project\|local` (default user), `--sparse <paths…>`, `--claudeai`, `--json` | one JSON result line |
| `plugin marketplace list` | `--json` | JSON array `[{name, source, path\|repo, installLocation}]` |
| `plugin marketplace remove\|rm <name>` | `--scope` (omit = every scope), `--json` | one JSON line |
| `plugin marketplace update [name]` | `--json` (only with a name) | one JSON line |
| `plugin install\|i <plugin[@mkt]>` | `--scope` (default user), `--marketplace <source>` (adds it first), `--config k=v`, `--registry`, `-y`, `--accept-command <sha256>`, `--json` | one JSON line |
| `plugin uninstall\|remove <plugin>` | `--scope`, `--keep-data`, `--prune` (+`-y`), `--json` | one JSON line |
| `plugin update <plugin>` | `--scope` (auto), `-y`, `--accept-command`, `--json`. Help: "restart required to apply" | one JSON line |
| `plugin enable <plugin>` / `disable [plugin]` | `--scope` (auto); `disable -a/--all`; `--json` | one JSON line |
| `plugin list` | `--json`, `--available` (needs `--json`), `--data-size` | JSON |
| `plugin details <name>` | none probed | text only: component inventory + projected token cost |
| `plugin configure`, `validate`, `prune`, `tag`, `eval`, `init`, `test` | (out of scope) | |

**JSON result line.** Every mutating `--json` command prints
`{"command", "outcome": "ok"|"failed", "plugin"|"marketplace", "pluginId", "scope", "message", "failureCode"?}`.
`update` adds `updateOutcome` (`up_to_date`…), `oldVersion` and `newVersion`; `uninstall` adds `keptData`.

**Exit codes, as observed.**

```
marketplace add /tmp/d16-mkt          0  outcome ok ("declared in user settings")
marketplace add (again)               0  ok, "already on disk"  (idempotent)
install nosuch@d16-mkt                1  failureCode "not_found"
install hello@d16-mkt                 0  ok
disable / disable again               0 / 1  second: failureCode "already_in_goal_state", alreadyInGoalState:true
enable                                0
update hello@d16-mkt                  0  updateOutcome "up_to_date"
marketplace update d16-mkt            0
uninstall / uninstall again           0 / 1  failureCode "not_installed"
marketplace remove / again            0 / 1  failureCode "not_configured"
install hello --marketplace /tmp/d16-mkt   0  adds the marketplace, then installs
```

- **A no-op is exit 1.** A wrapper must key idempotency on `failureCode`, not on the exit code.
- On failure the human line `✘ Failed to …` also goes to stdout after the JSON line. Parse only the first line.

### 1.2 Where the state lives (probe, then read back)

After `marketplace add` and `install`, in the config dir:

- **`settings.json`** (user scope) gains `extraKnownMarketplaces.<name>.source` and `enabledPlugins["hello@d16-mkt"]:
  true`. `enable`/`disable` flip that boolean.
  - Schema @280967035: `enabledPlugins` is "plugin-id@marketplace-id" → bool or a version constraint. Precedence is
    user < project < local < flag < policy.
  - `additionalMarketplaces` is an alias of `extraKnownMarketplaces` (@280969439).
- **`plugins/known_marketplaces.json`**: `{name: {source, installLocation, lastUpdated, autoUpdate?}}` (schema `T_`
  @280830283). A `directory` source is read in place (`installLocation` = the source path). Git sources are cloned
  under `plugins/marketplaces/<name>/`.
- **`plugins/installed_plugins.json`**: `{"version": 2, "plugins": {id: [{scope, installPath, version, installedAt,
  lastUpdated}]}}`.
- **Cache dir.** `plugins/cache/<marketplace>/<plugin>/<version>/` holds a copy of the plugin. Persistent data lives
  in `plugins/data/<id>/` (uninstall `--keep-data`).
- **Catalog caches.** `plugins/plugin-catalog-cache.json` (539 KB: per-plugin components and token costs for the
  official marketplace) and `plugins/plugin-directory-cache-v2.json` (2.7 MB, §1.4). Both are fetched on the first
  `plugin list --available`.

### 1.3 Sharing across accounts and hosts

- **`SHARE_ALLOWLIST`** (`src/viberpit/configdir.py:29`) is `{"plugins", "commands", "settings.json",
  "settings.local.json", "projects", "file-history", "CLAUDE.md"}`. **Both `plugins/` and `settings.json` are
  shared**: in `~/.claude-accounts/cfg/tony_steinonline.net/` both are symlinks into `shared_base()` = `~/.claude`.
  `output-styles/` is **not** shared (§3), and neither is `.claude.json`.
- **Symlink probe.** Two account dirs `acctA` and `acctB` had `settings.json` and `plugins` symlinked into a shared
  dir.
  - `CLAUDE_CONFIG_DIR=acctA claude plugin marketplace add … && install …` wrote through both symlinks. **Both links
    survived**: the CLI does not replace a symlinked `settings.json` with a regular file.
  - `acctB plugin list --json` saw the plugin at once, and `acctB plugin disable` wrote the shared file.
  - **So an install from any account dir reaches every account on that host.**
- **Caveat: `installPath` is absolute and names the account dir that ran the install**
  (`/tmp/d16-sym/acctA/plugins/cache/…`). The live dobby registry already mixes `cfg/tony_cairnea.com/…`,
  `cfg/tony.claude_steinonline.net/…`, `~/.cache/ccswitch/helper-cfg/…` and `~/.claude/…` paths. They resolve only
  while that account dir (or helper dir) exists. **A ViberPit install should run with `CLAUDE_CONFIG_DIR=
  shared_base()`**, so the recorded path is `~/.claude/plugins/cache/…` and survives account removal.
- **Hosts are separate.** rocky has its own `~/.claude/plugins` (2.1.293). Its installed and enabled sets match dobby's
  today only because they were installed by hand on both. The edge's library API reads only the **edge's**
  `shared_base()`. An install made through ViberPit on dobby does not reach rocky worker sessions unless it is
  fanned out over the daemon RPC.
- **Live sessions.** Probe B (`run4.py`): `claude plugin install` from outside a running stream-json CLI, then a
  `reload_plugins` control request.
  - The reply listed `hello` in `plugins[]` and `hello:hi` in `commands`.
  - The plugin's output style appeared as **`hello:Pirate`** in `reload_output_styles` only **after**
    `reload_plugins`.
  - `reload_plugins` (@283016339) takes `hold_on_cache_impact` (refuse when the tool list change would break the
    prompt cache) and `add_plugins`.
  - **Install, update and enable can therefore reach a live session without a respawn**, at a prompt-cache cost.
    `plugin update`'s help still says "restart required".
- **Project-declared plugins are not auto-installed headless.** Probe A: a cwd `.claude/settings.json` declaring
  `extraKnownMarketplaces` + `enabledPlugins` for the local marketplace. After `initialize` and 4 s, no `hello:*`
  command, no `plugin_install` frame and no cache entry appeared. (`CLAUDE_CODE_SYNC_PLUGIN_INSTALL` @157326219 is
  the opt-in for headless installs. ViberPit does not set it.)

### 1.4 Browsing without installing

- **`claude plugin list --json --available`** returns `{installed:[…], available:[{pluginId, name, description,
  marketplaceName, version, source}]}` for every known marketplace, **including a built-in one that is never added**:
  `anthropic-plugin-directory` ("Anthropic Directory", `In` @280709735).
  - With an empty config dir it listed **3,605** directory plugins (7.2 MB of output, 44k lines). It fetched them from
    `/api/directory/plugins` (@284110802) into `plugin-directory-cache-v2.json` (TTL constants `Ke=3600000`,
    `ze=604800000` @306780206).
  - After `marketplace add`, the local `hello@d16-mkt` row appeared too, with `source: "./plugins/hello"`.
- **Directory review state** (from the cache's `checks.review`):
  - **3,163 of 3,605 have `state: "none"` (unreviewed)**;
  - 350 are `done` by `partner`;
  - 92 are `done` by `anthropic`.
  - Every listing pins a `reviewed_commit` SHA. `surfaces` includes `claude_code` for all of them.
- **A marketplace's own catalog** is `<installLocation>/.claude-plugin/marketplace.json` (`directory` sources read in
  place; git sources under `plugins/marketplaces/<name>/`). It can be read without installing anything.
- **Detail before install.** `plugin details <id>` printed the inventory (Skills/Agents/Hooks/MCP servers/LSP
  servers) and the token cost only for an **installed** plugin; pre-install use was not probed (Gaps). For the
  official marketplace, `plugin-catalog-cache.json` already holds `components` and `tokens` per plugin.
  `server/library/scan.py:61 _catalog()` reads it.

### 1.5 Trust and supply chain (binary)

- **The CLI's only consent is copy in the interactive UI.**
  - Copy @164663288/@327322099: "Make sure you trust a plugin before installing, updating, or using it. Anthropic does
    not control what MCP servers, files, or other software are included in plugins…"
  - **Headless `plugin install --json` asks nothing** for an ordinary plugin (probe: exit 0, no prompt).
  - The only gate is a **marketplace-declared command** (a `command`-source plugin, or a `headersHelper` that fetches
    the archive). That needs `-y`, or `--accept-command <sha256>` of the exact command a previous `--json` run
    reported as `shownCommand.sha256`.
  - `disableCommandPluginSources` (@280970869) turns that source type off.
- **Policy gates are managed-settings only.**
  - `strictKnownMarketplaces` @280969852: "When set in managed settings, ONLY these sources can be added… The check
    happens BEFORE downloading". Owner wildcard `{"source":"github","repo":"owner/*"}`, plus a `hostPattern` form.
  - `blockedMarketplaces` @280970869.
  - A sentinel entry allows or blocks the built-in Directory (@280811075).
  - `strictPluginOnlyCustomization` @151398332 closes the `--plugin-dir`/`--mcp-config` CLI-flag bypass.
  - ViberPit installs no managed-settings file today. The repo's `.claude/managed-settings.json` is a project file
    that SDLC governance checks (`server/sdlc/governance.py:21`), not the CLI's managed tier.
- **Auto-update.** `L2e` @280773176: a marketplace auto-updates "on startup" by default when:
  - it is in the official set `Ggt` (`claude-plugins-official`, `anthropic-marketplace`, … minus
    `knowledge-work-plugins`/`first-party-plugins`);
  - or its source is `claudeai`;
  - or its source is `pluginDirectory`.
  - **Installed official plugins therefore update themselves without an install action.** Third-party marketplaces
    do not, unless `autoUpdate: true`.
- **Name-squatting guard.** `VQe` @280773176 rejects marketplace names impersonating official ones ("official
  anthropic", "claude-marketplace"…, with NFKC and zero-width normalisation).
- **What a plugin can run:** hooks (shell on every matching event, in the session's process tree), MCP servers
  (stdio processes), LSP servers, agents and skills. ViberPit sessions run in `full-auto` for automations, so a
  plugin hook runs unattended with the account's credentials and the worktree's push rights.
- **Risks that need an owner decision (§6, D1–D3):**
  1. The Directory is mostly **unreviewed** (88%) and is browsable with zero configuration. A "browse and install"
     UI over `--available` puts 3k unreviewed plugins one tap from every session on every account.
  2. `marketplace add` accepts any GitHub repo, git URL, https URL or path. A marketplace is itself a moving target:
     `marketplace update` re-reads it, and official ones auto-update.
  3. An install from the web UI is a remote-code-execution primitive for whoever holds a tailnet identity. It
     already is one through the MCP add form (`PUT /library/mcp/{name}` with a stdio command), so this is not new in
     kind, but it is new in reach.

### 1.6 ViberPit today

- **Server.** `server/api/library.py`:
  - `GET /library/plugins` (`:107`) → `scan.list_plugins()` (`scan.py:86`): registry ∪ `enabledPlugins`, catalog
    enrichment, and `marketplaces` from `known_marketplaces.json` (name, source, lastUpdated).
  - `GET /library/plugins/{id}` (`:112`) → `scan.plugin_detail` (`scan.py:209`).
  - `PATCH /library/plugins/{id} {enabled}` (`:122`) → `mutate.set_plugin_enabled` (`mutate.py:51`). It writes
    `shared_base()/settings.json` **directly** (a second writer beside the CLI), with a pre-mutation snapshot and an
    audit row `library_plugin_toggled`.
  - **`GET /library/marketplace` (`:587`, issue #596) is dead and wrong.**
    - It reads `plugin-catalog-cache.json` as `data.get("plugins", data)`, but the real shape is
      `{version, fetchedAt, catalog: {plugins: {…}}}`. It therefore returns the **whole cache object** as `plugins`,
      and `lastUpdated` is always null (the file has `fetchedAt`).
    - No web code calls it (`grep /marketplace web/src` = 0 hits).
    - D-16 must replace or delete it (Principle VI).
- **Web.**
  - `web/src/features/settings/sections/PluginsSection.tsx` (132 lines): a list with an enable switch
    (`useTogglePlugin`) and an expandable detail (`useLibraryPluginDetail`). Marketplaces are one line, "Marketplaces:
    a, b".
  - There is no install, uninstall, update or browse.
  - Hooks: `web/src/api/hooks.ts:1865-1893`. Registry: `settingsMeta.ts:77` and `settingsDefs.ts:83` (`scope:
    'server'`, `layout: 'wide'`).
- **Engine gating.** `GET /library/engines` (`library.py:67`) states that Codex has no library
  (`CODEX_LIBRARY_UNSUPPORTED_DETAIL`).

## 2. MCP per-tool toggles

### 2.1 Binary: there is no per-tool switch, but deny rules work and are live

- **No per-tool key.** A sweep of the binary for `*disabled*Tool*`/`*Mcp*Enabled*` found only
  `disabledMcpServers`/`enabledMcpServers` (server level, `Do()` @294073252, stored in the **project entry of
  `.claude.json`**), `disabledMcpjsonServers` and `enabledMcpjsonServers` (approval of `.mcp.json` servers), and the
  generic `disallowedTools`.
- **`mcp_toggle {serverName, enabled}`** (@283030027) is per **server** only. In probe 1 it worked live: status
  `disabled` with 0 tools, then `connected` with 3. It also **persisted** `disabledMcpServers: ["probe"]` into
  `<cfg>/.claude.json` `projects[cwd]`, the per-account file that is not shared. ViberPit's `trust_project` already
  overwrites that list at every spawn (`configdir.py:278`).
- **`mcp_set_servers`** replaces the SDK's dynamic server set. `set_mcp_permission_mode_override` is `@internal`
  (Chrome). Neither works per tool.
- **Per-tool disable = a permission deny rule `mcp__<server>__<tool>`.** In probes 1, 3 and 6 the tool was **removed
  from the tool pool** (it disappears from `get_context_usage.mcpTools`), not merely denied at call time. Rule
  forms:
  - `mcp__probe__tool_b`: that tool;
  - `mcp__probe` or `mcp__probe__*`: every tool of the server (a session-scoped server-off with no `.claude.json`
    write).

| Mechanism | Probe | Live? | Reversible live? |
|---|---|---|---|
| `apply_flag_settings {permissions:{deny:[…]}}` (no spawn deny) | 1, 3 | **yes**: the next `get_context_usage` shows the change | **yes**: a later patch replaces it (deny `[b]`→`[c]` shows a,b; `deny:[]` or `permissions:null` restores all) |
| `--settings {"permissions":{"deny":[…]}}` at spawn | 6 | spawn | **no**: a live `deny:[]` leaves it; a live `deny:[c]` gives `[b, c]` (arrays concatenate onto the spawn value) |
| `--disallowedTools` / SDK `disallowed_tools` | 2 | spawn | **no**: source `cliArg`, editability `session`, untouched by flag patches |
| project/user `settings.json` `permissions.deny` | 5 | file | n/a. Rules from every source are unioned (`projectSettings` + `flagSettings` both listed) |

- **Merge semantics** of `apply_flag_settings`, which matter for every flag-layer feature (D-14 fast/effort, D-16
  output style and denies):
  - a patch's **top-level key replaces the previously applied value of that key**: `{permissions:
    {additionalDirectories:[…]}}` dropped an applied `permissions.deny` (probe 3, step 8);
  - other top-level keys are kept: `outputStyle` survived a `permissions` patch;
  - scalars override the spawn value: spawn `outputStyle: Learning`, applied `NoSuchStyle` → effective
    `NoSuchStyle`;
  - arrays concatenate onto the spawn value.
  - **So ViberPit needs one composer that owns the whole `permissions` object it sends**, and it must never put a
    reversible deny in the spawn `--settings`.

### 2.2 SDK 0.2.105

- `ClaudeAgentOptions.allowed_tools` (auto-allow) and `disallowed_tools` ("removed from the model's context") map to
  `--allowedTools`/`--disallowedTools`, spawn only (`subprocess_cli.py:257,265`).
- `settings` maps to `--settings`; `add_dirs` to `--add-dir`; `plugins` to local plugin dirs only.
- `ClaudeSDKClient` methods: `get_mcp_status`, `toggle_mcp_server`, `reconnect_mcp_server`, `get_context_usage`,
  `set_permission_mode`, `set_model`, `rewind_files`, `stop_task`, `interrupt`, `get_server_info`.
- **There is no method for** `apply_flag_settings`, `reload_plugins`, `reload_output_styles`, `list_permission_rules`
  or `mcp_set_servers`. These go through `_query._send_control_request`, the seam D-9 and D-14 already use
  (`claude_driver.py:2393`).

### 2.3 Listing a server's tools

- **`mcp_status`** returns `mcpServers[]` (schema `Ro` @282527717). Each row has `name`, `status`
  (`connected|failed|needs-auth|pending|disabled`), `serverInfo`, `config`, `scope`, `transport` and **`tools:
  [{name, description, annotations: {readOnly, destructive, openWorld}, _meta}]`**. With `liveReply` and an oversize
  reply it falls back to `toolCount`. **Deny rules do not hide tools from `mcp_status`**: after deny `tool_b` it still
  listed all three (probe 1), so it is the stable list to render switches from.
- **`get_context_usage`** (`detail: "summary"` avoids token-count API calls) returns `mcpTools: [{name:
  "mcp__probe__tool_a", serverName, tokens, isLoaded}]` (@282949425). It reflects the **effective** pool after
  denies, which makes it the read-back to confirm a toggle.
- **With no live session there is no tool list.** `claude mcp get <name>` health-checks but prints no tools (probe).
  ViberPit's `probe_mcp` (`orchestrator.py:21689`, behind `POST /library/mcp/test`) spawns a no-prompt CLI per host
  and polls `mcp_status`, so it can carry the tools. **But `sanitize_statuses` (`mcp_health/status.py:97`) reduces
  `tools` to `tool_count`**, and `McpServerState` (`:33`) has no names. Neither the Settings page nor the session rail
  sees tool names today.

### 2.4 ViberPit today

- **Server-level scope already exists** (spec "repo-scope"; `server/mcp_scope/`):
  - per-server default, per-repo rule and **per-session override** (`store.py:14-74`);
  - resolved by `resolve.effective_disabled`, then `apply.disabled_for_session` (`apply.py:15`);
  - written to `.claude.json` `disabledMcpServers` at spawn (`configdir.trust_project`, called at
    `orchestrator.py:2708`);
  - re-enforced once per CLI start with `toggle_mcp_server` (`claude_driver._enforce_disabled_mcp`, `:2698`).
  - Live per-session toggle: `POST /sessions/{id}/mcp/{server}/toggle` (`api/sessions.py:2422`) →
    `orchestrator.mcp_toggle` (`:21643`, which persists the session override) → `driver.toggle_mcp_server`
    (`:2605`).
  - Kill switch: `mcp_scope_enabled`.
  - UI: Settings › MCP `McpScopeControl` (`McpSection.tsx:180`) and the session `McpServerList.tsx:32`
    (`useToggleSessionMcp`, `api/mcpScopeHooks.ts:46`), shown in the Context card through `DeferredMcpInlineList`
    (`rail/ContextCard.tsx:141`).
- **A per-tool layer would sit beside it**, with the same three-level shape (server default, optional repo, session
  override), keyed `(server, tool)`. It would be applied **live after `initialize`** through `apply_flag_settings
  {permissions: {deny: [...]}}`, never through `--settings` (§2.1 floor). The natural spot is next to
  `_enforce_disabled_mcp`, awaited before the first prompt is written. Persistence is a new table, or JSON columns on
  the `mcp_scope` store; a new model means the Postgres-suite lists (`_REQUIRED_EXTRAS`, `RECONCILE_EXEMPT`).
- **No conflict with existing deny plumbing.**
  - ViberPit sends **no `permissions` key** in the flag layer today. `_spawn_flag_settings` (`orchestrator.py:8082`)
    emits only `fastMode`/`effortLevel`, and `spawn_settings` (`claude_sdk_factory.py:52`) adds
    `enableAllProjectMcpServers`.
  - `server/sdlc/governance.py` `MANAGED_DENY` only checks the repo file `.claude/managed-settings.json` (a project
    file). Project deny rules are unioned with flag denies (probe 5), so a flag deny cannot weaken them.
  - The one hazard is internal: once D-16 owns the flag `permissions` key, any later feature that wants
    `permissions.additionalDirectories` (`/add-dir`) must go through the same composer, or it silently drops the tool
    denies (probe 3 step 8).

## 3. Output styles

### 3.1 Binary and probes

- **Setting.** `outputStyle` is a free string, "Controls the output style for assistant responses" (@280980778). The
  default name is `default`.
- **Built-in styles.** `initialize.available_output_styles` lists `default, Proactive, Concise, Explanatory,
  Learning` in that order (probe 1). These are the four `repl_main_thread:outputStyle:*` names @151265249, plus
  `default`.
- **Custom styles.**
  - User styles live in **`<CLAUDE_CONFIG_DIR>/output-styles/*.md`**. `initialize.user_output_styles_dir` reports the
    path.
  - Project styles live in `<cwd>/.claude/output-styles/*.md`.
  - Frontmatter keys:
    - `name` ("Style name used in the Output style picker… Defaults to the filename", @154916722);
    - `description`;
    - `keep-coding-instructions` ("If true, the default coding instructions stay in the system prompt alongside this
      style");
    - `force-for-plugin` (plugin styles only; ignored with a warning elsewhere).
  - Probe 1 listed the user file `terse.md` as `Terse` (its `name`) and the project file `proj.md` as `proj` (the
    filename).
- **Plugin styles** are named `<plugin>:<name>` (`hello:Pirate`, probe B). They appear after `reload_plugins` +
  `reload_output_styles`.
- **`initialize` reports it.** `output_style` (current) and `available_output_styles` (@299641708). No other control
  request lists styles.
- **`reload_output_styles`** (@283018937) re-reads the style dirs and returns `{available_output_styles}`, with no
  description.
- **At spawn.** `--settings {"outputStyle":"Learning"}` → `initialize.output_style: "Learning"` (probe 2).
- **Live.** `apply_flag_settings {outputStyle: "Explanatory"}` → success; `get_settings.effective.outputStyle:
  "Explanatory"` (probe 1).
  - **An unknown name is accepted silently**: `NoSuchStyle` → success, effective `NoSuchStyle` (probe 2). ViberPit
    must validate against `available_output_styles`.
  - The style reaches the model as a system-prompt section plus a per-turn `output_style` attachment (@293510928,
    @293535867). Changing it calls `QH("output_style")`, a prompt-assembly invalidation (@280227267), so expect a
    prompt-cache miss on the next turn.
- **The CLI's own `/output-style`** (`h8e` @291875680) is `supportsNonInteractive` and **is in the SDK command list**
  (`initialize.commands` contains `output-style`).
  - Its handler (@305893857) writes `Kn("localSettings", {outputStyle})`, which is **`<cwd>/.claude/
    settings.local.json` inside the session worktree**.
  - `update_settings` (@283038961) has the same `localSettings` + `outputStyle` allowlist.
  - Under a flag-layer value the local write is overridden (precedence local < flag).
  - **It must not pass through** (§4).

### 3.2 ViberPit today

- There is no output-style concept: `grep outputStyle|output_style|output-style` hits only
  `server/library/snapshots.py:47`, which snapshots the `output-styles` dir.
- `output-styles` is **not** in `SHARE_ALLOWLIST`, so a custom style written into one account dir is invisible to the
  others. No account and no `~/.claude` has an `output-styles/` dir today.
- The shared `settings.json` enables `learning-output-style@claude-plugins-official`. That plugin works through a
  SessionStart hook, not an `outputStyle`.
- **Mechanism (mirrors D-14).**
  - A nullable `sessions.output_style` column plus an `AppSettings` default (migration **0102**; the latest is
    `0101_mcp_grant_health.py`).
  - `_spawn_flag_settings` emits `outputStyle`. A live change goes through `driver.apply_flag_settings`.
  - The list comes from `server_info.available_output_styles`, validated server-side.
  - Settings › Output styles lists the styles (built-in, user, plugin) and edits user style files in the shared dir
    (needs `output-styles` added to `SHARE_ALLOWLIST`). The `+` menu gets a "Style ›" submenu (`PlusMenuEntry.submenu`,
    `PlusMenu.tsx:22`).

## 4. App slash commands

- **Catalog** (`server/sessions/commands/catalog.py`):
  - `CLIENT` `:58` = help, context, mcp, fork, rewind, btw, fast;
  - `APP_ONLY` `:68` = fast, effort;
  - `CONTROL` `:70` = model, clear, effort;
  - `EXCLUDED` `:73` includes `add-dir`;
  - `APP_COMMANDS` `:144`; dispatcher `dispatcher.py:26`.
- **Of D15's list, `/fork /rewind /btw /effort /fast /model` already exist as app commands. Only `/plan` is
  missing.**
- **`/plan` in the CLI** is `local-jsx` with `requires:{ink:!0}` (`Jnr` @292189009), "Enable plan mode or view the
  current session plan", `[open|<description>]`. **It is not in the SDK command list** (`initialize.commands` has no
  `plan`), so today a typed `/plan` is sent to the model as plain text.
- **Plan mode in ViberPit.**
  - `POST /sessions/{id}/permission-mode` (`api/sessions.py:2491`) → `orchestrator.set_permission_mode` (`:24159`) →
    `ClaudeAgentDriver.set_permission_mode` (`claude_driver.py:2291`).
    - That driver method treats plan as an **overlay**: it remembers `_pre_plan_mode`, and `default` returns to it.
    - It sends `set_permission_mode` to the CLI. Probe 1: `{mode:"plan"}` → `{"mode":"plan"}`; `default` →
      `{"mode":"default"}`.
  - The UI control is `ModePicker` (`composer/ModePicker.tsx:37`, used in `PromptRow.tsx:86`; `PLAN_OPTION` in
    `session/permissionModes.ts:25`).
  - The WS `setPermissionMode` frame and the Assistant tool `set_permission_mode` (`server/assistant/tools.py:1859`)
    also reach it.
  - Codex runs plan as its read-only sandbox (`permissions.py:27`, `MODE_WIDTH`).
- **What `/plan` should dispatch to.** A `control` app command (`argument_hint "[on|off]"`) that calls the same
  `orchestrator.set_permission_mode(sid, "plan" | "default")`:
  - bare `/plan` toggles;
  - `default` restores the pre-plan mode through the existing overlay.
  - Put it in `APP_ONLY` so a future CLI headless `/plan` never passes through.
  - `/plan <description>` is decision D6.
- **`/output-style`** must join `APP_ONLY` once output styles ship (§3.1: the CLI copy writes into the worktree). Add
  an app `/output-style [style]` that drives the same action as the `+` menu.
- **`/add-dir` (not in D15).**
  - The CLI supports it mid-session: `apply_flag_settings {permissions:{additionalDirectories:[dir]}}` → success, and
    `list_permission_rules.workspaceDirectories` shows it (probe 2; reported with source `localSettings`, though no
    file was written: probe 3 step 8 found no file in cwd).
  - `register_repo_root {directory}` exists too (@283018937: "Add a directory as a working…"; probe: refused an
    already-registered dir). The non-interactive `/add-dir` (`zXe` @291864415) is gated off (absent from
    `initialize.commands`).
  - ViberPit has no surface; `add-dir` is `EXCLUDED`.
  - **Recommend out of D-16.** If built later, it must share the flag `permissions` composer (§2.4).

## 5. Code map (file:line)

| Feature | Server | Web |
|---|---|---|
| Plugins | `api/library.py:107,112,122` (list/detail/toggle), `:587` (dead marketplace); `library/scan.py:45,61,86,209`; `library/mutate.py:51`; `configdir.py:29` | `settings/sections/PluginsSection.tsx`; `api/hooks.ts:1865-1893`; `settingsMeta.ts:77`; `settingsDefs.ts:83` |
| MCP per server | `api/library.py:228` (GET), `:367-404` (scope), `:267` (probe); `mcp_scope/{store,resolve,apply}.py`; `api/sessions.py:2399,2422`; `orchestrator.py:2708,21643,21689`; `claude_driver.py:2591,2605,2618,2698` | `sections/McpSection.tsx:180` (`McpScopeControl`); `sessions/McpServerList.tsx:32`; `api/mcpScopeHooks.ts:46`; `rail/ContextCard.tsx:141`; `settingsMeta.ts:83`; `settingsDefs.ts:86` |
| MCP tool names | `mcp_health/status.py:33,97` (drops `tools`); `mcp_health/store.py:26` | `api/types.ts` (`McpServerState`) |
| Flag layer | `claude_sdk_factory.py:49,52,173`; `orchestrator.py:8082` (`_spawn_flag_settings`), `:8134` (`set_fast_mode`); `claude_driver.py:2378` (`apply_flag_settings`); `flag_settings.py`; `models.py:334` (`fast_mode`) | `composer/FastToggle.tsx`; `PromptRow.tsx:52` |
| Output styles | (new) column + AppSettings + `_spawn_flag_settings`; `api/sessions.py:2535` is the pattern (`/fast`) | `composer/PlusMenu.tsx:22,31`; `Composer.tsx:404`; `DispatchBar.tsx:663`; new Settings section in `settingsMeta`/`settingsDefs` |
| Slash commands | `commands/catalog.py:58,68,70,73,144,268`; `commands/dispatcher.py:26` | palette consumes `build_catalog` |
| `/plan` | `api/sessions.py:2491`; `orchestrator.py:24159`; `claude_driver.py:2291`; `assistant/tools.py:1859` | `composer/ModePicker.tsx:37`; `session/permissionModes.ts:25` |

Housekeeping: the bundle ceilings (`web/bundle-budget.json`) are entryJs **109.7**, initialJs **306.2** and
initialCss **15.6** (gzip kB). Assistant tools: 114 registered and 107 exposed
(`docs/product/features/ai-assistant.md:143`). No open PR touches these files (GraphQL check, 2026-11-04).

## 6. Recommended slicing and owner decisions

### 6.1 Slicing

- **D-16a: output styles + `/plan` + `/output-style`.** This is small, reuses D-14's flag layer end to end, and has no
  security surface.
  - Migration 0102 (`sessions.output_style`, `app_settings.default_output_style`).
  - `_spawn_flag_settings` emits `outputStyle`; `POST /sessions/{id}/output-style`; validation against
    `available_output_styles`.
  - Settings › Output styles (list, default, user-style editor in a shared `output-styles/`) and the `+` menu "Style ›".
  - `/plan [on|off]` and `/output-style [style]` as `APP_ONLY` app commands, plus a contract test pinning
    `available_output_styles`/`output_style` and the live `apply_flag_settings` read-back.
- **D-16b: MCP per-tool toggles.**
  - Carry `tools[]` (name, description, annotations) through `sanitize_statuses`/`McpServerState`, and persist the last
    seen list per server from `probe_mcp` and live polls.
  - A per-tool rule store with server default and session override.
  - One flag-`permissions` composer applied live after `initialize`, with read-back through `get_context_usage
    .mcpTools`.
  - Settings › MCP expands a server into tool switches; the Context card's MCP list expands likewise.
  - Contract test: a deny removes the tool from `mcpTools`, and `deny:[]` restores it.
- **D-16c: plugin marketplace.** It is gated on D1–D3.
  - A `claude plugin … --json` wrapper run with `CLAUDE_CONFIG_DIR=shared_base()` that parses the first line and
    keys on `failureCode`.
  - Fan-out to every host through the daemon RPC.
  - Marketplaces add/remove/update; browse from `marketplace.json` / `list --available`; install, uninstall, update.
  - A consent card showing hooks and MCP servers, an audit row per mutation, and a `reload_plugins` offer for live
    sessions.
  - Route enable/disable through the same wrapper, so the CLI is the single writer of `enabledPlugins`.
  - Delete `GET /library/marketplace`.
  - A kill switch `VIBERPIT_PLUGIN_INSTALL_ENABLED`, default off until D1 is settled.

### 6.2 Owner decisions

**D1. Which plugin sources can ViberPit install from (security).**
- (a) **(Recommended)** **Owner-curated marketplaces only.** Settings › Plugins adds and removes marketplaces; each add
  shows its source and needs an explicit confirm.
  - Install is offered only from those marketplaces, plus the Anthropic Directory filtered to `review.state ==
    "done"`.
  - `command`-source and `headersHelper` plugins are refused: ViberPit never passes `-y`/`--accept-command`.
  - The trade-off: some friction, and community plugins need a marketplace add first.
- (b) Official only: `claude-plugins-official` plus the reviewed Directory entries, and no marketplace add from the
  UI. Safest, but it drops the spec's "marketplaces (add/remove)".
- (c) Anything the CLI accepts, including 3,163 unreviewed Directory plugins and arbitrary git URLs, with a warning
  dialog. This is closest to Desktop, and it makes the web UI a one-tap path for third-party code to run under
  full-auto automations.

**D2. Install scope across hosts.**
- (a) **(Recommended)** Fan out every install, update, uninstall and marketplace change to every host's daemon, and
  report per-host outcomes. rocky worker sessions see the same plugins as dobby. This costs a new RPC and partial
  failure handling.
- (b) Edge host only. Simpler, but rocky sessions drift silently, and the Plugins page shows dobby's state as if it
  were the fleet's.

**D3. Plugin auto-update.**
- (a) **(Recommended)** Keep the CLI default: official marketplaces and the Directory auto-update on startup, others
  do not. Show the version and "auto-updates" on each row. This changes nothing.
- (b) Pin everything: write `autoUpdate: false` per marketplace, and updates happen only through the UI's Update
  button. Updates are reproducible and reviewed, but security fixes in official plugins arrive late.

**D4. How a per-tool deny is layered.**
- (a) **(Recommended)** Live-only: apply every per-tool deny (Settings default ⊕ session override) through
  `apply_flag_settings` right after `initialize`, awaited before the first prompt. A session can re-enable a tool the
  Settings default disabled.
- (b) Settings defaults at spawn (`--settings`), session toggles live. The default is enforced from the first byte,
  but a session **cannot** re-enable a tool the default disabled (spawn denies are a floor, §2.1), and Context-card
  switches for those tools would be read-only.

**D5. Where the default output style lives.**
- (a) **(Recommended)** A ViberPit `AppSettings` default applied through the flag layer per session. It never touches
  the shared `settings.json`, so the owner's own terminal `claude` is unaffected.
- (b) Write `outputStyle` into the shared `settings.json`. It is one place, but it changes every account **and**
  interactive CLI use on the host, and the per-session picker still needs the flag layer.

**D6. `/plan` arguments.**
- (a) **(Recommended)** `/plan [on|off]` only: bare toggles, and it mirrors `/fast`.
- (b) Also `/plan <description>`: enter plan mode and send the text as the prompt (CLI parity). It is a little more
  dispatcher work, and it is a command that also sends a prompt.

## Gaps (not verified)

- Whether a **live** `outputStyle` change alters the next turn's output. Only `get_settings` read-back and the code
  path were checked; confirming it needs a model turn.
- `plugin details` for a plugin that is **not installed**, and the network behaviour of `plugin list --available`
  under `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (the probe ran without it).
- `marketplace add` against GitHub or git sources, and `--sparse`. Only a local `directory` source was probed (no
  network clone).
- What `plugin install` does with a plugin whose hooks or MCP servers need `userConfig` (`--config`) and none is
  given.
- ~~`reload_plugins` with `hold_on_cache_impact` on a session with a real prompt cache. In the probe the session had no
  conversation.~~ Resolved by binary read in `2026-11-05-d16c-residuals-verification.md` §1 (the hold needs
  `totalOutputTokens() > 0` and no tool search). The held reply itself is still unobserved.
- Whether `mcp_status` with `liveReply` drops `tools` for many-server configs on stdio hosts. The schema says only
  CCR transports do.
- Whether the CLI auto-installs project-declared plugins after the workspace-trust dialog, which ViberPit pre-accepts
  through `trust_project`. Probe A saw no install within 4 s; longer waits were not tried.
- None of the plugin or output-style probes ran on rocky 2.1.293, only the `--help` and string counts in §0.
