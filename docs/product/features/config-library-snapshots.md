---
slug: config-library-snapshots
title: Config Library & Snapshots
state: live
links:
  code: [server/library/scan.py, server/library/mutate.py, server/library/hooks.py, server/library/snapshots.py, server/library/names.py, server/api/library.py, server/api/presets.py, server/api/permission_profiles.py, server/api/hooks.py, server/api/snapshots.py, web/src/components/library/PluginsSection.tsx, web/src/components/library/SkillsSection.tsx, web/src/components/settings/PresetsSection.tsx, web/src/components/settings/PermissionProfilesSection.tsx, web/src/components/settings/HooksSection.tsx, web/src/components/settings/SnapshotsSection.tsx]
  prs: ['#560', '#565', '#605', '#606', '#877', '#232']
---

# Config Library & Snapshots

**Problem** — A ccswitch operator runs many `claude` sessions across several accounts, all sharing
one config base (`CLAUDE_CONFIG_DIR`). The reusable configuration that shapes those sessions —
installed plugins, skills, slash commands, MCP servers, lifecycle hooks, permission profiles, and
spawn presets — was opaque and hand-edited across scattered JSON/markdown files. There was no way to
see what was installed, safely mutate it, template it into reusable presets/permission-profiles, or
roll back a bad edit. A single fat-fingered `settings.json` change could silently break every
session, and there was no undo.

**Target users** — The power user / operator configuring how sessions run: enabling plugins,
authoring skills and slash commands, wiring MCP servers, editing hooks, and defining named presets
and permission profiles that are applied across accounts and at session spawn.

**Desired outcome** — A browsable "library" of the shared config that can be scanned, named,
mutated, and applied safely. Read routes are direct filesystem scans that degrade gracefully
(a missing or malformed file yields an empty result / `parseError` flag, never an exception).
Mutations are atomic (tempfile+rename, secure 0600 writes) and audited. Every mutation takes a
throttled best-effort snapshot first, so any change is reversible; snapshots can be listed,
diffed, restored, exported, and imported. Reusable templates — session presets and permission
profiles (named, ordered tool-pattern allowlists applied at spawn) — sit on top of the same base.
Hooks can be validated and dry-run in a light sandbox, and enabled/disabled without deletion via a
`disabledHooks` parking block.

**Success metrics** — (unknown — needs owner input). Observable candidates the design supports:
snapshot/restore events recorded in the audit log; number of named presets and permission profiles
in active use; reduction in config-mutation errors thanks to pre-mutation auto-snapshots.

**Non-goals** — Not a credential/identity manager: credential- and identity-bearing files
(`.credentials.json`, `.credentials`, `.claude.json`) are NEVER captured in a snapshot/export nor
written on restore/import — the single `is_excluded` predicate gates both directions and blocks
tar-slip (`server/library/snapshots.py`, constitution II/IV). Not a security sandbox for hooks —
hook validation is a light sanity gate, not a boundary; hooks run arbitrary host shell. Does not
walk the multi-GB `plugins/cache/` tree — only shallow component dirs of installed plugins.

**Notes** — State is `live`. The suite landed incrementally: the Library tab (plugins/skills/
commands/MCP servers) in #560, tab consolidation into a Settings tab in #565, presets + permission
profiles + templates + marketplace in #605 (issues #595–#600), and the power-user config suite —
hooks, schedules, notifications, snapshots — in #606 (issues #601–#604). Auto-snapshot-before-
mutation traces to issue #604 (see the `# #604` note in `server/library/mutate.py`). MCP
cross-account paste-back OAuth was hardened in #877; the "never prompt for MCP tools" permissive
setup came via #232. Snapshots are tar.gz under `paths.root()/"snapshots"` with a JSON metadata
sidecar; auto snapshots are throttled (5s) so a burst of mutations collapses to one.

**Evidence** — `server/library/scan.py` (read-side: plugins/skills/commands/MCP scans that never
raise); `server/library/mutate.py` (atomic write-side; `snapshot_before_mutation()` guard on every
mutation); `server/library/hooks.py` (hooks read/validate/write + sandboxed `dry_run`, `disabledHooks`
parking); `server/library/snapshots.py` (`is_excluded` credential/tar-slip gate, create/list/diff/
restore/export/import); `server/library/names.py` (identifier validation / path-traversal defense);
`server/api/library.py`, `server/api/presets.py` (issue #597), `server/api/permission_profiles.py`
(issue #600), `server/api/hooks.py`, `server/api/snapshots.py`;
`web/src/components/library/{PluginsSection,SkillsSection,CommandsSection,McpSection}.tsx`,
`web/src/components/settings/{PresetsSection,PermissionProfilesSection,HooksSection,SnapshotsSection}.tsx`.
PRs: #560 (Library tab), #565 (Settings-tab consolidation), #605 (presets/templates/permission
profiles), #606 (hooks/schedules/notifications/snapshots), #877 (MCP auth), #232 (permissive MCP setup).
