# D-13 "Memory files": verification spike

Date: 2026-11-03 (spike ran 22:00 to 22:25 EST; the file is dated for the slice). Constitution III: every claim
below was checked against the installed binary, by a no-model probe, or in ViberPit's code. Claims that were not
checked are listed under **Gaps**.
Parent spec: `2026-11-01-ui-desktop-parity-design.md` lines 56, 340, 366, 375 and 402 (decision `[A-D8]`).
Earlier spikes: `2026-11-02-d8-fork-verification.md`, `2026-11-03-d9-rewind-verification.md`.

## 0. Versions and how to read the offsets

- **Binary.** `~/.local/bin/claude` resolves to `/mnt/ssk-ssd/tony/cache/.local/share/claude/versions/2.1.318`.
  - `--version` prints `2.1.318 (Claude Code)`, and its sha256 starts with `708c77b204db3008`.
  - Every byte offset below (`@N`) is into this file. The live daemon's sessions run the same file (`/proc/<pid>/exe`).
- **The bundle is minified.** One name can mean different things in different chunks, so each offset was read in
  context.
- **Python SDK.** `claude-agent-sdk` is in the deploy venv (`/home/tony/GitHub/viberpit-deploy/.venv`, Python 3.14).
  ViberPit always passes `cli_path`, so 2.1.318 is the binary that matters.
- **Probes.** All probes are under §5. No user message was ever written, no `assistant` frame appeared, and no model
  call was made. Each probe ran with an env built from scratch, a throwaway `CLAUDE_CONFIG_DIR` and `HOME`, and
  `ANTHROPIC_BASE_URL=http://127.0.0.1:9`, which is the D-9 harness. Every probe dir was deleted afterwards.

## 1. What the CLI loads, and in what order

**Loader.** The loader is `_Wo` @291448800, which `ov()` memoizes. Its file list is the session's `userContext`
memory set: `PWo` @~291464800 calls `ov(e,!1,n)`, and the result is stored as `userContextMemoryFiles`.

**Path helpers.**
- `iee()` @283461262:
  ```js
  case"User":return rt(Ue(),"CLAUDE.md"); case"Local":return rt(ve(),"CLAUDE.local.md");
  case"Project":return rt(ve(),"CLAUDE.md"); case"Managed":return rt(LS(),"CLAUDE.md")
  function eQe(){return rt(LS(),".claude","rules")}  function Aht(){return rt(Ue(),"rules")}
  ```
- `Ue` @280251198 is `(Lan() ?? join(homedir,".claude")).normalize("NFC")`, and `Lan()` is
  `process.env.CLAUDE_CONFIG_DIR` @280250800.
- **So the user-level file is `$CLAUDE_CONFIG_DIR/CLAUDE.md`** when that var is set, and `~/.claude/CLAUDE.md` only
  when it is not. The user rules dir is `$CLAUDE_CONFIG_DIR/rules/`. Probes A and E confirm both.
- Managed `LS()` is `getManagedFilePath()`, which defaults to `/etc/claude-code` on Linux (`ZC()` @~281120100).
  That path does not exist on dobby.

**Order, as the code pushes it** (`_Wo` body @291448800 to 291453500):
1. **Managed.**
   1. `/etc/claude-code/CLAUDE.md`.
   2. The remote-policy `claudeMd` (`DPr()`), with path `<policyHelper>`.
   3. `policySettings.claudeMd`.
   4. `/etc/claude-code/.claude/rules/*`.
   - Managed loads are not gated on `--setting-sources`.
2. **User** (only if `qt("userSettings")`).
   1. `$CLAUDE_CONFIG_DIR/CLAUDE.md`, plus its imports.
   2. `$CLAUDE_CONFIG_DIR/rules/*`.
3. **The project walk.** It runs from the filesystem root down to the cwd, and stops before `/` itself
   (`for(yo=ft; yo!==root; yo=dirname(yo)) bt.unshift(yo)`). For each dir:
   1. `CLAUDE.md`, then `.claude/CLAUDE.md`, then `iXt(dir)`. `iXt` is an empty list today (`T6e=[]` @291425193),
      and AGENTS.md is only probed for telemetry.
      - Gated on `uI("projectSettings")` @291434606.
   2. `.claude/rules/*`.
   3. `CLAUDE.local.md`, gated on `localSettings`.
   - When the cwd is a git worktree nested inside its main repo, dirs between the two are not marked project
     (`G5t` / `z5t` @~291447000).
4. **`--add-dir` dirs.** Each gets the same three files. This is gated by `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`
   (@280411855). Probe H passed `--add-dir` without the env var, and nothing from that dir loaded.
5. **AutoMem.** This is the `MEMORY.md` index of `<cfg>/projects/<slug>/memory/`, when auto-memory is on.

**Imports (`@path`).**
- **Parser.** `cWo` @291440032 matches `(?:^|\s)@((?:[^\s\\]|\\ )+)` in markdown text tokens only.
  - It skips `code`/`codespan` tokens and HTML comments.
  - It strips `#fragment`.
  - It accepts `./`, `~/`, absolute paths and bare names.
  - Each path is resolved against the dir of the importing file (`pt(H, dirname(n))`).
  - Only text extensions load (`sWo` @291436132).
- **Depth.** `oQr=5` @291440810, and `S8t` refuses `depth >= 5`. That means the root file plus 4 levels of import.
  Probe A loaded `d1`..`d4` from `CLAUDE.md → d1 → … → d6`, and not `d5` or `d6`.
- **External imports.** A path outside the cwd (`GU`) loads only if `includeExternal` is set.
  - User files always pass `!0` (true).
  - Project and Local files need `hasClaudeMdExternalIncludesApproved` (@283404623), the per-project approval.
  - In probe A, `@~/homeimport.md` and an absolute out-of-tree path loaded from the **user** file. `@../../outside/
    projext.md` from the **project** file did not.
- **Code spans.** `` `@incode.md` `` did not load (probe A).
- **Dedupe.** Paths are deduped (`processedPaths`), including symlink targets: `iV` @291442095 adds both `cp(e)` and
  `cp(resolvedPath)`.

**Not loaded at startup** (probe A):
- A rule with `paths:` frontmatter (`.claude/rules/cond.md`). It is a conditional rule, attached when a matching file
  is read.
- A nested child `sub/CLAUDE.md` below the cwd.

**`--setting-sources` gates memory directly** (probes A to D):

| flag | loaded |
|---|---|
| (absent) | user + user rules + user imports, project walk, rules, local |
| `""` | **nothing**: `memoryFiles: []`, and the "Memory files" category disappears |
| `user` | user file, its imports, user rules |
| `project,local` | project walk, rules, local; no user file |

The SDK (`subprocess_cli.py:352`) emits `--setting-sources` only when it is set. ViberPit passes none, as the live
argv shows, so sessions load everything. The comment at `server/sessions/claude_sdk_factory.py:44-46` ("the binary
loads no filesystem settings") is wrong for this binary and should be corrected.

**Other switches** (not probed):
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS` (@280395154) short-circuits the rule loaders.
- `claudeMdExcludes` (a glob list in settings, `g8t` @~291440600) filters User, Project and Local paths.

## 2. The `~/.claude/CLAUDE.md` mystery: it loads as a *Project* file

`server/sessions/scratch_config.py:256` and `server/assistant/prompt.py:18` say the user file "is resolved from HOME,
not from `CLAUDE_CONFIG_DIR`". §1 shows the CLI resolves it from `CLAUDE_CONFIG_DIR`. Two things explain why
ViberPit's sessions still see `~/.claude/CLAUDE.md`:

- **No account cfg dir has a `CLAUDE.md`.** `ls ~/.claude-accounts/cfg/*/CLAUDE.md` matches nothing, and
  `SHARE_ALLOWLIST` (`src/viberpit/configdir.py:26-27`) does not share it. So every session's **User** slot is empty.
- **The project walk passes through `$HOME`.** Every worktree lives under `/home/tony`. At the dir `/home/tony` the
  walk tries `/home/tony/.claude/CLAUDE.md` as the `.claude/CLAUDE.md` candidate, and loads it as **type Project**.
  - Probe A shows it: its root sat under `/home/tony/.cache/claude-tmp`, and `Project 958 /home/tony/.claude/CLAUDE.md`
    appeared after the user rows.
  - This session's own context labels the file "project instructions, checked into the codebase".
- **A cwd outside `$HOME` loses the user instructions entirely.** Probe 4 used the real `HOME=/home/tony`, an empty
  account cfg and a cwd under `/tmp`. Its only memory row was the project's own `CLAUDE.md`.
  - Any repo outside `/home/tony` is affected today, for example `/mnt/ssk-ssd/tony/GitHub/KinDash` or `/mnt/ssk-ssd/
    tony/GitHub/infra` opened by path, not through a `/home` alias.
  - **This is a live bug that D-13's sync fixes for free** (§4).
- **Side effect today.** The operator's global instructions are presented to the model as repo-checked-in project
  instructions, and the `/memory` dialog files them under kind `other`, not `user`.

## 3. Learning the loaded set at runtime, with no model call

| Source | Lists memory files? | Notes |
|---|---|---|
| stream-json `system/init` | **No, and never sent without a turn.** No `system` frame appeared in any control-only probe. `init` carries no memory field either: it was not in the `initialize` response keys. | |
| `initialize` control response | **No.** Keys: `account, agents, analytics_disabled, available_output_styles, capabilities, claude_code_version, commands, current_permission_mode, fast_mode_*, feedback_mode, hook_on_failure_supported, ide_rc_auto_enable_gate, models, output_style, pid, remote_control_*, session_state, user_output_styles_dir` | |
| **`get_context_usage`** (schema @282922706, response `n1` @282949229) | **Yes: `memoryFiles: [{path, type, tokens}]`** in load order, with absolute paths and the type `Managed`/`User`/`Project`/`Local`/`AutoMem` | Public in the Python SDK (`ClaudeSDKClient.get_context_usage()`, `types.py:787`). Takes `detail: "summary" \| "full"`, default `full`. |
| **`get_memory_dialog`** (schema @282933770; `@internal`) | **Yes, as UI rows**: `files[{kind: user\|project\|project-alt\|local\|other, path, label, description, exists}]`, `folders`, `memories`, `auto_memory`, `auto_dream` | Lists the user and project rows **even when they are missing or not loaded** (probe B). Per its description it **"clears the session's memory-file cache first"**. Not in the Python SDK. |
| `/memory` command | **Not in headless.** It is `{type:"local-jsx",name:"memory",thinClientDispatch:"control-request"}` @291876689, which is the TUI form of `get_memory_dialog`. `initialize.commands` (76 entries) lists `context` and `init` but not `memory`. | |
| `/context` | The same data as `get_context_usage`. ViberPit's `/context` already returns that dict (`server/sessions/commands/dispatcher.py:31`). | |

**Response shape** (probe A, `get_context_usage` with `detail:"summary"`; `full` gave the same `memoryFiles`):
```json
{"memoryFiles":[
  {"path":"<root>/cfg/CLAUDE.md","type":"User","tokens":36},
  {"path":"<root>/home/homeimport.md","type":"User","tokens":7},
  {"path":"<root>/outside/userext.md","type":"User","tokens":7},
  {"path":"<root>/cfg/rules/u.md","type":"User","tokens":3},
  {"path":"/home/tony/.claude/CLAUDE.md","type":"Project","tokens":958},
  {"path":"<root>/home/CLAUDE.md","type":"Project","tokens":7},
  {"path":"<root>/home/.claude/CLAUDE.md","type":"Project","tokens":8},
  {"path":"<root>/home/proj/CLAUDE.md","type":"Project","tokens":24},
  {"path":"<root>/home/proj/other.md","type":"Project","tokens":4},
  {"path":"<root>/home/proj/d1.md","type":"Project","tokens":5}, "… d2, d3, d4 …",
  {"path":"<root>/home/proj/.claude/CLAUDE.md","type":"Project","tokens":6},
  {"path":"<root>/home/proj/.claude/rules/r.md","type":"Project","tokens":4},
  {"path":"<root>/home/proj/CLAUDE.local.md","type":"Local","tokens":4}],
 "categories":[…,{"name":"Memory files","tokens":1088},…], "totalTokens":…, "maxTokens":…, "...": "…"}
```

**Notes on the shape.**
- `memoryFiles` carries **no parent link**. An import is indistinguishable from a root file except by position and
  type.
- `get_memory_dialog` gives the tree. Each `label` is indented by depth (`"L d1.md"`, `"  L d2.md"`) and the
  description says `"@-imported"`.
- The path is the **link path, not the resolved target**. With `cfg/CLAUDE.md` a symlink, the row is
  `<root>/cfg/CLAUDE.md` (probes F and G).
- **`detail:"full"` counts each category through the token-count API.** That is a network request, not a turn. With
  no auth it fell back silently, and the probe still returned. `summary` answers locally and gave identical
  `memoryFiles`. The Python SDK's `get_context_usage()` sends no `detail`, which means `full`.

## 4. CLAUDE_CONFIG_DIR, `SHARE_ALLOWLIST` and the sync design

- **Today:**
  - `SHARE_ALLOWLIST = {"plugins","commands","settings.json","settings.local.json","projects","file-history"}`
    (`src/viberpit/configdir.py:26-27`).
  - `SHARED_STATE_DIRS = {"file-history"}` (`:33`).
  - `CLAUDE.md` is not shared, and no account has one.
- **`reconcile_links`** (`:113-205`) links each allowlisted entry to `paths.shared_base()/<name>`
  (`src/viberpit/paths.py:57-79`, normally `~/.claude`), but **only if the source exists** (`:160`).
  - A real file shadowing a shared entry that is not in `SHARED_STATE_DIRS` is **deleted** (`:182-186`).
- **The CLI accepts a symlinked user file.**
  - Probe F: `cfg/CLAUDE.md` pointed at another file, and the CLI loaded it as `User` with its imports.
  - Probe G: `cfg/CLAUDE.md` pointed at `$HOME/.claude/CLAUDE.md` with the cwd under `$HOME`. The CLI loaded it **once**,
    as `User`. The walk's `$HOME/.claude/CLAUDE.md` row was deduped.
  - The `iV` symlink refusal (`h===0&&isSymbolicLink()`) applies only when `includeExternal` is false for User. That is
    the `local-agent` entrypoint (`w8t()` @~291447700), which ViberPit never uses.
- **Recommendation: add `CLAUDE.md` to `SHARE_ALLOWLIST`.** Do not write a copy into each account dir.
  - **One file, one writer.** The symlink is the single source of truth (Principle VI).
    - The edit endpoint writes only `shared_base()/CLAUDE.md`.
    - Every account sees the edit on its next file read. There is no fan-out write, no drift and no partial-failure
      state.
  - **It fixes §2.**
    - The file becomes type `User`, so the model is told the truth about it.
    - It loads for a cwd **outside `$HOME`**.
    - It is still loaded exactly once for a cwd inside `$HOME` (probe G).
  - **Cross-host comes free.** Rocky mounts both `~/.claude-accounts` and `~/.claude` from dobby over sshfs
    (`docs/runbooks/rocky-session-host.md:26-27,39-47,121-131`). A symlink in an account dir that points at
    `/home/tony/.claude/CLAUDE.md` resolves, on rocky, to the same sshfs-backed file.
    - There is no sync code to write, and none exists today: no rsync or push in `server/` or `src/`.
    - Caveats: a dropped mount silently falls back to rocky's local files (runbook `:140`), and FUSE attribute
      caching once caused torn reads (#1005). So write atomically: temp file plus `os.replace` in the same dir. A
      reader then sees the old or the new file, never half of one.
  - **Two changes the allowlist needs:**
    1. **Do not let `reconcile_links` delete a private `CLAUDE.md`.** None exist today, but an interactive
       `claude` run under an account cfg could create one, for example through `/memory`. Treat it as user content:
       keep it private and log `share_kept_private`, or append it to the shared file once. Never unlink it. A
       one-line `SHARED_STATE_FILES` set, or a branch next to `SHARED_STATE_DIRS`, covers it.
    2. **"Only if the source exists"** means creating `~/.claude/CLAUDE.md` from the UI does not link the accounts
       until the next `compose()`, which runs on every spawn (`launcher.py:133`, `mcp_auth.py:307`). That is fine for
       new sessions. Live sessions never re-read the User slot anyway (see Gaps).
  - **Assistant scratch config.** `server/sessions/scratch_config.py:93` `ISOLATED_EXCLUDED_SHARED` decides whether
    the assistant gets the file. It inherits it today through the HOME walk, and `prompt.py` overrides it. Keep that
    behaviour: leave `CLAUDE.md` out of the exclusion set, and fix both comments to name the HOME walk, not the HOME
    resolution, as the reason.
- **Rejected: write a copy into each account dir.**
  - It means N writes per edit on every host, and a new account starts with no copy.
  - A partial failure leaves accounts disagreeing, so it needs a reconcile loop to re-converge.
  - Interactive edits through one account's `/memory` fork the copies. It is the parallel-implementation shape.

## 5. Probe log (trimmed)

- **Setup** (`/tmp/d13-scripts/probe.py`, deleted). The tmp root was under `$TMPDIR=/home/tony/.cache/claude-tmp`:
  - `home/` is `HOME`, holding `.claude/CLAUDE.md`, `CLAUDE.md` and `homeimport.md`.
  - `cfg/` is `CLAUDE_CONFIG_DIR`, holding `CLAUDE.md` (imports `@~/homeimport.md` and an absolute `outside/userext.md`)
    and `rules/u.md`.
  - `home/proj/` is the cwd, holding:
    - `CLAUDE.md`, which imports `@other.md`, `@../../outside/projext.md`, `` `@incode.md` `` and `@d1.md`, where
      d1→…→d6 is a chain.
    - `.claude/CLAUDE.md`, `.claude/rules/{r.md, cond.md (paths: src/**)}`, `CLAUDE.local.md` and `sub/CLAUDE.md`.
- **Frames.** `initialize`, `get_context_usage{detail:summary}`, `get_memory_dialog`, `get_context_usage` (full), then
  stdin closed.
- **A (no flag):** 16 rows, as in §3. Not loaded: `d5`, `d6`, `projext.md`, `incode.md`, `cond.md` and
  `sub/CLAUDE.md`.
  - `get_memory_dialog` `files` (trimmed):
    ```
    user        <root>/cfg/CLAUDE.md          "User instructions"   "Saved in ~/.claude/CLAUDE.md"
    other       <root>/home/homeimport.md     "L ~/homeimport.md"   "Saved in ~/.claude/CLAUDE.md"
    other       /home/tony/.claude/CLAUDE.md  "/home/tony/.claude/CLAUDE.md"  ""
    project     <root>/home/proj/CLAUDE.md    "Project instructions" "Saved in ./CLAUDE.md"
    other       <root>/home/proj/d2.md        "  L d2.md"           "@-imported"
    project-alt <root>/home/proj/.claude/CLAUDE.md ".claude/CLAUDE.md" ""
    local       <root>/home/proj/CLAUDE.local.md   "CLAUDE.local.md"   ""
    folders: [{kind:auto, path:<root>/cfg/projects/<slug>/memory/, label:"Open auto-memory folder"}]
    auto_memory: {enabled:true, toggleable:true, status:"on"}
    ```
    The description string says `~/.claude/CLAUDE.md` even when the file is `$CLAUDE_CONFIG_DIR/CLAUDE.md`. It is a
    display string, not a path.
- **B (`--setting-sources ""`):** `memoryFiles: []`, and the dialog still lists `user` and `project` with `exists:true`.
- **C (`user`):** the 4 User rows only.
- **D (`project,local`):** the 12 Project and Local rows. The dialog puts the `user` row last.
- **E (no `CLAUDE_CONFIG_DIR`):** the first row is `User 8 <root>/home/.claude/CLAUDE.md`, and the walk's copy of that
  path is deduped.
- **F (`cfg/CLAUDE.md` → `shared/CLAUDE.md`):** the same as A.
- **G (`cfg/CLAUDE.md` → `$HOME/.claude/CLAUDE.md`):** `User 8 <root>/cfg/CLAUDE.md`. There is no `Project` row for
  `<root>/home/.claude/CLAUDE.md`.
- **H (G plus `--add-dir extra/`, which holds a `CLAUDE.md`):** `extra/CLAUDE.md` was not loaded.
- **Probe 4:** real `HOME=/home/tony`, an empty cfg and the cwd under `/tmp`. The only row was
  `Project 3 <root>/cwd/CLAUDE.md`, with **no** `/home/tony/.claude/CLAUDE.md`.

## 6. Code map

- **The D-6 Context card** (`web/src/features/sessions/rail/ContextCard.tsx:39-98`) shows:
  - The context meter, from `contextPct`, plus Compact now.
  - The inline MCP list.
  - `SessionRulesReadout`.
  - Quota windows.
  - **Nothing about memory.**
- **Where its data comes from.**
  - `contextPct` is `remainingFromContextUsage(state.contextUsage)` (`useSessionRuntime.ts:640`), and the reducer
    stores `{usedTokens, maxTokens}` (`reducer.ts:1293-1303`).
  - The server source is `ClaudeAgentDriver.context_usage()` → `get_context_usage()` (`claude_driver.py:2299-2305`).
  - `_emit_context_usage` (`:2577-2603`, called after each turn settles at `:1784`) **keeps only `totalTokens`,
    `maxTokens`, `percentage` and `model`, and drops `memoryFiles`**.
  - The event is `ev.ContextUsage` (`server/sessions/events.py:481-490`, wire `contextUsage`). It is persisted by
    `orchestrator._persist_context_usage` (`orchestrator.py:7865`) and replayed on attach (`bridge/ws.py:993`,
    `:1473`).
  - The `/context` command returns the whole dict (`commands/dispatcher.py:31`), but `CommandResultOverlay.tsx:50-56`
    reads only three fields.
  - `grep memoryFiles|memory_files` over `server/` and `web/src` finds nothing.
- **Viewing a file** (D-7): `GET /sessions/{id}/files/content?path=` (`server/api/sessions.py:2163`) →
  `worktree_files.read_file` (`server/sessions/worktree_files.py:273-311`). The path rules:
  - The cwd must be a git worktree.
  - `worktree_rel` (`permission_rules/patterns.py:259`) does a realpath plus `commonpath` containment check, and fails
    closed on `..`, `~` and NUL.
  - `.git` is refused.
  - `_open_beneath` follows no symlink anywhere in the path.
  - Only regular files are read, up to 512 KiB (`truncated` past that).
  - There is no secret redaction.
- **It cannot show user-level or out-of-tree memory files.** They return `PATH_OUTSIDE_WORKTREE`, and so does an
  in-repo `CLAUDE.md` that is a symlink.
  - In fleet mode it is the `session_file` RPC to the owning daemon (`fleet_orchestrator.py:211`, `remote.py:928-940`,
    `ipc/orchestrator_rpc.py:322`).
  - The viewer is `web/src/features/sessions/files/FileViewer.tsx` (`useSessionFile`, `api/hooks.ts:1436`).
  - Spec line 366 assumed this endpoint is "the memory-file viewer"; it is that only for in-worktree rows.
- **Settings › Claude config exists.** It is group `claude` (`web/src/features/settings/settingsMeta.ts:24`), with
  sections plugins, skills, commands, mcp, hooks and snapshots (`:40-89`).
  - There is **no** CLAUDE.md, memory or instructions editor in `web/src/features/settings` or `server/api`.
  - The closest pattern is the Library API: GET/PUT/DELETE `/skills/{name}` and `/commands/{path}`
    (`server/api/library.py:136-196`), using `server/library/scan.py` (reads under `shared_base()`),
    `server/library/mutate.py` (writes) and `names.resolve_under` (containment).
  - Config snapshots already capture `CLAUDE.md` (`server/library/snapshots.py:34`), so an edit through `mutate` gets
    the auto-snapshot.
- **Cross-host.** Rocky reaches both trees over sshfs, mounted by `viberpit-mounts.service` (`deploy/README.md:851`).
  A file written on dobby is visible on rocky while the mount is up. There is no sync code.
- **Housekeeping.**
  - The newest migration is `0096_session_views` (`down_revision = "0095_session_rewinds"`).
  - Bundle ceilings (`web/bundle-budget.json`): entryJs **107.6**, initialJs **301.7**, initialCss 15.2 kB gzip.
  - Assistant tools: **114** registered and **107** default (`docs/product/features/ai-assistant.md:143`), pinned by
    `server/tests/test_assistant_conformance.py:183`. A new read tool means updating both numbers, and see the
    "parallel assistant-tool PRs conflict on counters" note.

## Gaps (not verified)

- **Whether `memoryFiles` grows mid-session** when a conditional rule or a nested `sub/CLAUDE.md` is attached on file
  read. The rows come from `userContextMemoryFiles`, the start-of-session set (@~291463600), so the attachments
  probably do not appear. Proving it needs a turn that reads a file, and none was spent.
- **Whether a live session picks up an edited user file.** `ov()` is memoized per session, and the system-prompt
  `userContext` is built once. A hold epoch (`$W()`) and `get_memory_dialog` clear the memo. The expected answer is
  "next session, or after `/clear` / respawn", but it was not probed with a turn.
- The Managed rows, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `claudeMdExcludes`, and `--add-dir` with
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` were read in the code only, not probed.
- **External project imports after approval.** `hasClaudeMdExternalIncludesApproved` lives in the project's
  `.claude.json` entry, and the approval flow was not exercised.
- **Codex.** AGENTS.md discovery was not examined. D-13 should be Claude-only behind a capability flag.
- **Rocky** was not probed directly. The cross-host claim rests on the runbook plus symlink semantics.

## Implementation implications

**Listing loaded files (Context card).**
- Read `memoryFiles` from `get_context_usage`. It is the CLI's own loaded set, in order, with tokens, and it is the
  public SDK surface (constitution I: no internal request needed).
  - Extend `_emit_context_usage` to carry `memoryFiles` (path, type, tokens) on `ev.ContextUsage`.
  - Persist it next to the reading so attach replay shows it. Updating it after each turn is free, since the call
    already happens.
  - Consider `detail:"summary"` for the post-turn pull: it skips the per-category token-count requests and gives the
    same memory rows. Doing that needs a raw control request; the SDK method sends no `detail`.
- **Do not use `get_memory_dialog` for the card.**
  - It is `@internal`.
  - It clears the session's memory cache on every call.
  - It lists files that were not loaded (probe B).
  - It is the right source only if a later slice wants the `/memory` tree labels.
  - Parent and child structure: rows after a root file with the same type and a path that root imports are its
    imports. If the UI needs to show nesting, re-run `cWo`-equivalent parsing server-side on view, not at list time.
- **Labelling.** Show `User`, `Project`, `Local`, `Managed` and `AutoMem`, with the path made relative to the worktree
  where it is inside it, and `~`-relative otherwise.
  - Before the sync ships, `/home/tony/.claude/CLAUDE.md` arrives as `Project`. Either ship the allowlist change
    first, or special-case a `Project` row that is not under the worktree as "Inherited (home)".
- **The empty state.** Under `--setting-sources ""` the list is empty, which is legitimate. Codex has no rows.

**Viewing a file.**
- Rows inside the worktree reuse D-7 `files/content`.
- **Rows outside it need a new read path:** `GET /sessions/{id}/memory/content?path=`. Its rules:
  - Allow only a path that is **in that session's last reported `memoryFiles` set** (an allowlist, not a containment
    check). That is the security model: the CLI already chose to send these bytes to the model, so showing them to
    the session's owner discloses nothing new.
  - Read on the **owning host** through a daemon RPC, as `session_file` does, because rocky's paths are rocky's.
  - Apply the same 512 KiB cap and the regular-file rule.
  - Follow symlinks here, since the user row is a symlink. Re-check the resolved path is a regular file.
- **Repo `CLAUDE.md` stays read-only.** Every viewer row is read-only, and only the Settings editor writes.

**Settings › Claude config › Memory.**
- Add a `memory` section to the `claude` group.
- Add `GET`/`PUT /api/library/memory` (or `/claude-md`), editing **only** `shared_base()/CLAUDE.md`.
  - Write atomically (temp file plus `os.replace`).
  - Size-cap it.
  - Take the existing config auto-snapshot, which already covers `CLAUDE.md`.
  - Audit the write.
- Add `"CLAUDE.md"` to `SHARE_ALLOWLIST`, with a keep-private rule for an existing real file (§4), plus a test in the
  existing configdir test file.
- Fix the comments at `claude_sdk_factory.py:44-46`, `scratch_config.py:256` and `assistant/prompt.py:18`.
- **Copy for the editor:**
  - "Applies to new sessions on every account and host."
  - "Imports (`@path`) are followed up to 4 levels."
  - (Live-session pickup is a gap; do not promise it.)
- No migration is needed if the memory rows ride the persisted context reading as JSON on an existing column. A new
  nullable column is picked up by reconcile, but add a revision `0097_…` chained off `0096_session_views`. *(Shipped as `0098_session_memory_files`, chained off `0097_host_platforms` after #3245.)*
- **An assistant tool is optional.** If one is added, it is a read of `/api/library/memory`, and both counts
  (114/107) move.
