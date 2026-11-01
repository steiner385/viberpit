# Claude Desktop — Code tab: sourced feature inventory (as of 2026-11-01)

Purpose: benchmark ViberPit's web UI against the Claude Desktop app's Code tab.
Method: official docs fetched 2026-11-01 (raw markdown from code.claude.com/docs/en/*.md), plus the
Claude Code CHANGELOG (latest entry v2.1.316). Secondary sources are labelled **[secondary]**.
"Not documented" means the official pages fetched do not describe it — not that it doesn't exist.

Key source abbreviations:
- **D** = https://code.claude.com/docs/en/desktop (main Code tab reference, ~209 KB)
- **DQ** = https://code.claude.com/docs/en/desktop-quickstart
- **DST** = https://code.claude.com/docs/en/desktop-scheduled-tasks
- **RC** = https://code.claude.com/docs/en/remote-control
- **WEB** = https://code.claude.com/docs/en/claude-code-on-the-web
- **MOB** = https://code.claude.com/docs/en/mobile
- **PM** = https://code.claude.com/docs/en/permission-modes
- **CL** = https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md

Platform baseline (D, DQ): macOS (Universal, Intel + Apple Silicon), Windows (x64 and ARM64; MSIX for
enterprise), Linux **beta** (Debian/Ubuntu apt/.deb, amd64/arm64; no Fedora/RHEL; no Computer Use;
https://code.claude.com/docs/en/desktop-linux). Requires Pro, Max, Ultra, Team or Enterprise. Desktop
bundles Claude Code — no Node/CLI install needed. Two UI generations coexist: "current layout" and
"earlier layout" (D § Check which layout you have); many features differ by layout. Pane layout,
terminal, file editor and view modes require Desktop v1.2581.0+.

---

## 1. App shell & information architecture

| Item | Description | Source | Status / caveats |
|---|---|---|---|
| Code tab | Claude Code lives in the **Code** tab at the top of the app sidebar, next to chat and Cowork | DQ, D | GA, all OSes |
| Session model | Each conversation = a **session** with own history, project folder, context and changes | D § intro | |
| Sidebar session list | **+ New** (Cmd/Ctrl+N), list of sessions, run several in parallel | D § Work in parallel | |
| Group and sort | Current layout: hover a list heading → **Group and sort** icon; **Show** submenu (e.g. **Scheduled** group); **View all** → **Archived** tab. Earlier layout: **Filter** icon → **Group by** / **Sort by** / **Status → Archived** | D § Organize and find sessions; DST § Find a run's session | Exact group/sort options **not documented**; default grouping is implied to be by folder |
| Child sessions | Sessions started from a "suggested task" card are listed **under** the current session in the sidebar | D § Start a task that Claude suggests | current layout |
| Sidebar destinations | **Customize** (Connectors/Skills/Plugins), **Scheduled** (or **Routines**), **Projects**, a **More** menu | D § Extend; DST; claude-projects | Projects = public beta, Pro/Max/Ultra, gradual rollout |
| Split view | Option/Alt-click a session, or right-click → **Open in → Split view**; unfocused pane dims; Cmd+Ctrl+W / Ctrl+Alt+W exits | D § Work in parallel | |
| New window | Cmd/Ctrl-click a session or **Open in → New window** | D | |
| Session title bar | Rename by clicking title; pane buttons (**Terminal**, **Changes**, **Browser**, **Files**); **⋮** overflow; caret session menu (**Transcript view**, **Open in**) | D § Arrange your workspace, § Organize | |
| Session details panel ("session rail") | Cards beside chat: **Changes** (diff counts, PR + CI), **Context** (%), **Environment** (incl. **Remote Control** switch), background work. **Hide/Show session rail** | D § See session details | Appears only when chat is wide enough |
| Panes | chat, diff, browser, terminal, file, plan, tasks, subagent, iOS Simulator (macOS). Drag header to move, drag edge to resize, Cmd/Ctrl+\ closes focused pane; overflow panes in **⋮** when narrow or pane has no button (e.g. **Background tasks**, **Plan**) | D § Arrange your workspace | v1.2581.0+ |
| Pop-out panes | Pop a pane (diff, terminal…) into its own window and dock it back | D | |
| Environment selector | First control under the prompt box: **Local**, **Cloud**, **SSH**, **WSL** groups; saved environments; **New environment** dialog; Desktop auto-creates up to 8 environments from recent folders | D § Start a session, § Environment configuration | WSL = Windows only |
| Projects (cloud) | One conversation where Claude coordinates parallel cloud threads; desktop notifications on posts/errors/needs-input | https://code.claude.com/docs/en/claude-projects | public beta |

## 2. Chat transcript rendering

| Item | Description | Source | Status |
|---|---|---|---|
| View modes | **Normal** (tool calls collapsed into summaries + full text), **Thinking** (+ Claude's thinking; menu item appears only after thinking exists), **Verbose** (every tool call, file read, intermediate step + thinking). Ctrl+O cycles; session menu → **Transcript view** | D § Switch view modes | Summary mode removed in v1.46388.1 |
| Diffs in transcript | In Manual mode "a diff showing exactly what will change" with approve/decline buttons per change | DQ § Review and accept changes | |
| `+N -M` indicator | Line-count chip opens diff view | DQ, D | |
| Server cards | Dev server started by Claude shows a card with **Open** | D § Preview your app | local only |
| Code review card | `/code-review` findings card grouped by file (**Walk through in diff**, **Apply fixes**) | D § Review your code | local/SSH/WSL; cloud + RC since v2.1.316 (CL) |
| Suggested-task card | **Start / Do it here / Options / Start all**; earlier layout: **Suggested task** card (**Send to cloud**, **Start with worktree**) | D § Start a task | |
| Cross-session message card | Incoming message shown as a card labelled with sending session's title + back link | D § Work across sessions | |
| Plugin suggestion card | Card listing suggested plugins with **Add** | WEB § Plugin suggestions | documented for cloud sessions |
| Permission cards | Site-action card (**Allow once / Always allow / Deny**); archive-session approval card | D | |
| Clickable file paths | Click a path to open it; HTML/PDF/video paths open in Browser pane; right-click → **Attach as context**, **Open in** (VS Code/Cursor/Zed), **Show in Finder/Explorer**, **Copy** (Absolute/Relative path/Filename) | D § Open and edit files, § Open files in other apps | |
| External links | First click asks Browser pane vs default browser; Cmd/Ctrl-click → default browser | D § Browse external sites | |
| Subagents | Main-conversation subagents always run in the background in Desktop; output viewed in tasks/subagent panes | D § Watch background tasks; CL v2.1.302 ("always run … in the background") | |
| Todos / task list rendering | **Not documented** for Desktop (CLI has a task list — interactive-mode § Task list) | — | |
| Thinking display | Only via Thinking/Verbose view modes | D | |
| Tool-call block visual design | **Not documented** beyond "collapsed into summaries" | D | |
| Streaming | **Not documented** explicitly; DQ mentions "real-time updates as Claude works" | DQ | |
| Images in transcript | Attach images/PDFs; rendering **not documented** | D § Add files | |
| Artifacts | Claude can publish artifacts from Desktop sessions | https://code.claude.com/docs/en/artifacts § Availability | Desktop v1.13576.0+, claude.ai account, Anthropic API only |
| Compaction | Auto-compaction when context fills; `/compact` manual; **idle compaction** of 200K+ idle conversations | D § Check and free up context; CL v2.1.314 | |

## 3. Composer

| Item | Description | Source | Status |
|---|---|---|---|
| Send / interrupt | **Enter** sends; stop button or **Esc** interrupts | D § Use the prompt box | |
| Steer mid-turn (queue) | Type a correction + Enter while running — read after the current action completes, without stopping | D, DQ | |
| Send now | A "Send now" action on a queued message exists | CL v2.1.292 ("Fixed Send now in the desktop app…") | UI **not documented** in D |
| Take back queued message | ✕ on queued message returns text to box | WEB § Take back a queued message | documented for cloud sessions; Desktop **not documented** |
| **+** menu | **Add files or photos** (WSL: **Add image** only), **Slash commands**, **Add connectors / Connectors**, **Add plugins / Plugins**, **Add folder** | D § Use the prompt box, § Add folders | Plugins not in cloud/WSL |
| Drag & drop | Images, PDFs, other files | D § Add files | |
| @-mentions | `@filename` with autocomplete | D; D § Feature comparison | Cloud: works only after first message |
| Slash command picker | Type `/` or **+ → Slash commands**: built-ins, custom skills, project skills, plugin skills; selected command shows highlighted in input | D § Use skills | |
| Suggested prompt | Gray ghost text after a reply; **Tab**/**Right arrow** accepts; off via **Prompt suggestions** setting | D § Accept a suggested prompt | costs a background request |
| Row under prompt box | Environment selector → project folder (+ **Worktree** toggle) → permission-mode selector → model picker (with **Effort** levels) → usage ring | D § Start a session, § Check usage | current layout |
| Model picker | Change any time mid-session; Cmd/Ctrl+Shift+I | D | |
| Effort menu | Under model picker; Cmd/Ctrl+Shift+E | D | Level names **not documented** in D (only `high` mentioned) |
| Permission mode selector | **Manual / Accept edits / Plan / Auto / Bypass permissions**; Cmd/Ctrl+Shift+M; remembered per folder (except Plan) | D § Choose a permission mode | see §6 |
| Fast mode | `/fast` documented for cloud sessions | WEB § Manage context | Desktop picker **not documented** |
| Dictation / voice | **Not documented** for the Code tab (`/voice` is a CLI terminal feature) | voice-dictation docs | — |
| Multiline, history search, vim | **Not documented** for Desktop | — | CLI-only per docs |

## 4. Commands & configuration

| Item | How Desktop exposes it | Source | Caveats |
|---|---|---|---|
| Built-in slash commands | Via `/` picker; text-output commands work; terminal-dialog commands with no argument form reply `isn't available in this environment` (e.g. `/permissions`); `/config` opens **Settings > Code / Claude Code** | D § What's not available | |
| Desktop-specific commands | `/code-review` (card UI), `/btw` (side chat), `/compact`, `/context`, `/remote-control`, `/resume` (pick up CLI sessions; local only), `/reload-plugins` (v2.1.260+) | D; commands docs | |
| Custom skills | Loaded from `~/.claude/skills/` (SSH: remote host's home); claude.ai-synced skills in local + cloud | D § Use skills | `disableSideloadFlags` can withhold |
| Plugins | GUI plugin browser: **Add plugins / Browse plugins / Manage plugins** (enable/disable/uninstall); user/project/local scope | D § Install plugins | not in cloud or WSL |
| MCP servers | **Connectors** UI (GUI MCP setup + directory; manage via **Customize > Connectors**); also `~/.claude.json`, `.mcp.json`, and `claude_desktop_config.json` (Desktop-only source; wins on name clash) | D § Connect external tools, § MCP servers from the Claude Desktop chat app | Connectors in local/SSH with claude.ai account |
| Customize hub | **Customize** sidebar item manages Connectors, Skills, Plugins; syncs via claude.ai (Cowork uses this, not `~/.claude`) | D § Extend Claude Code | |
| Hooks | No Desktop UI; hooks from settings files apply (shared config). Notification hooks fire for permission prompts under Desktop (fix in CL) | D § Shared configuration; CL | edit files directly |
| CLAUDE.md | Shared with CLI (`CLAUDE.md`, `CLAUDE.local.md`); no Desktop editor documented | D § Shared configuration | |
| settings.json | Same files as CLI; permission rules apply; `permissions.defaultMode` sets default mode; `env` in `~/.claude/settings.json` ignored when Desktop already set the var | D | file pane can't edit `.claude/settings.json` / `.mcp.json` (view-only) |
| Preview config | `.claude/launch.json` (`name`, `runtimeExecutable`, `runtimeArgs`, `port`, `cwd`, `env`, `autoPort`, `program`, `args`, `url`, top-level `autoVerify`) | D § Configure preview servers | |
| Local env editor | Gear on local environment: `KEY=value` lines, stored encrypted | D § Use the local environment editor | |
| Scheduled tasks | Prompts stored as `~/.claude/scheduled-tasks/<name>/SKILL.md` | DST | |

## 5. Settings surfaces

| Surface | Documented contents | Source |
|---|---|---|
| **Settings > Code** (or **Settings > Claude Code**, varies by version) | **Prompt suggestions**, **Allow bypass permissions mode** (Pro/Max/Ultra only), **Browser tools**, **Worktree location**, **Branch prefix**, **Auto-archive after PR merge or close**, **Connect new sessions to Remote Control**, **Use this computer from your phone and claude.ai** (+ folder list), **Keep this computer awake for Remote Control**; revoke site "Always allow" approvals | D; claude-projects |
| **Settings > Desktop app** or **Settings > This computer > System / Capabilities** | **Enable computer use**, macOS permission badges, denied apps, **Unhide apps when Claude finishes**, **Keep computer awake** | D § Computer use; DST |
| Browser pane **⋮** | **Keep cookies**, **Clear browsing data**, **Open links in built-in browser**, **Auto-verify changes** | D |
| File pane **⋮** | **Auto save** | D |
| Environment dialogs | New/edit environment (Local/SSH/Cloud tabs), SSH fields (Name, SSH host, port, key, **Worktree folder**), **Test connection** | D § SSH sessions |
| Scheduled task forms | Name, Description, instructions + mode + model, folder, Trigger presets, **Worktree**, Always-allowed panel | DST |
| Admin console / managed settings | Desktop / Cloud sessions / Remote Control toggles; keys incl. `disableAutoMode`, `permissions.disableBypassPermissionsMode`, `browserExternalPageTools`, `disableBrowserExternalNavigation`, `disableMobileSimulatorTools`, `sshConfigs`, `sshHostAllowlist`, `disableDesktopLocalSessions`, `forceLoginOrgUUID`; MDM plist / Windows registry / `/etc/claude-desktop/managed-settings.json` | D § Enterprise configuration |
| Full settings section list; theme; font size | **Not documented** | — |

## 6. User feedback

| Item | Description | Source | Status |
|---|---|---|---|
| Permission modes | **Manual** (`default`: asks before edits/commands, diff + accept/reject), **Accept edits** (`acceptEdits`), **Plan** (`plan`, session-only), **Auto** (`auto`, classifier; Opus/Sonnet 4.6+, Haiku 5.5, Fable), **Bypass permissions** (`bypassPermissions`; opt-in toggle on consumer plans, policy on Team/Ent) | D § Choose a permission mode | Cloud: Accept edits/Plan/Auto only; `dontAsk` CLI-only; Shift+Tab doesn't cycle |
| Critical-path `rm` in auto/bypass | Desktop denies it (bypass: unless a `PermissionRequest` hook allows) — no countdown prompt as in terminal | PM § critical paths; CL | |
| Plan approval | "Once you approve the plan, switch to Accept edits or Manual to execute it"; a **Plan** pane exists | D § Choose a permission mode, § Arrange | Approval UI **not documented** for Desktop (CLI: **Yes, and use auto mode / Yes, manually approve edits / No, keep planning**, Ctrl+G edit — PM § Review and approve a plan) |
| AskUserQuestion | **Not documented** in D. CL mentions a "question dialog" in "VS Code and the desktop and mobile apps" (mods tree above it) | CL v2.1.301 | |
| Site/computer-use prompts | **Allow once / Always allow / Deny**; computer use **Allow for this session / Deny** (30 min in Dispatch sessions) | D | |
| Requests while away (SSH) | Approval/question waits indefinitely on remote host; shown on rejoin | D § Handle requests that arrive while you are away | |
| OS notifications | When a session finishes a task and isn't in view; project notifications for project sessions; catch-up-run notification for scheduled tasks; SSH reconnect/login prompts | D § Get notified; DST § Missed runs | |
| Mobile push | Dispatch sessions push on finish/needs approval; Remote Control push (**Push when Claude decides**, **Push when actions required**) | D; RC § Mobile push notifications | |
| Context meter | **Context** card % with per-category breakdown; `/context` | D § Check usage | |
| Usage ring | Plan usage (shared across Claude Code surfaces) next to model picker | D § Check usage | |
| Session status badges in sidebar | **Not documented** (CL refers to statuses such as "waiting on a permission prompt" shown in desktop app) | CL | |
| Errors | Documented error states: "Failed to load session", Git/Git LFS required, 403, SSH failure cards with **View details** + codes (`[SFTP_UNAVAILABLE]`, `[SESSION_LIMIT]`, `[ENOSPC]`, `[CLI_UNRESPONSIVE]`…), disconnect banners ("Some output produced while you were disconnected may be missing here.") | D § Troubleshooting | |

## 7. Session lifecycle & branching

| Item | Description | Source | Status |
|---|---|---|---|
| New session | **+ New** / Cmd+N; choose environment, folder, mode, model before first message | D § Start a session | |
| Worktree isolation | **Worktree** toggle (off by default) → `<project>/.claude/worktrees/`; **Worktree location**, **Branch prefix**, `.worktreeinclude` | D § Run a session in its own worktree | requires Git |
| Add folders | Mid-session **+ → Add folder** (local/SSH); saved environments hold folders | D § Add folders | |
| Resume | Click session in sidebar; `/resume` lists CLI sessions on this machine (search title/folder/branch, preview); continues same session, not a copy | D § Coming from the CLI | local only |
| CLI → Desktop | `/desktop` (alias `/app`) or `claude --desktop [--continue \| --resume <id>]` | D; commands | macOS + x64 Windows, subscription only |
| Fork | Forking exists (Windows SSH limitation: "you can fork a session only at its first message") but the fork UI is **not documented** | D § Windows remote machines | |
| Rewind / checkpoints | **Not documented** for Desktop. CLI has `/rewind`/Esc Esc (checkpointing docs); cloud web has **Rewind to here** (WEB § Troubleshooting) | — | |
| Edit-and-resend | **Not documented** | — | |
| Side chat | Cmd/Ctrl+; or `/btw`; reads main thread, writes nothing back; not saved to disk | D § Ask a side question | local/SSH/WSL |
| Archive | Sidebar menu → **Archive**; **Archive anyway** on uncommitted worktree changes; **Unarchive** from **Archived** tab; worktree removed ~30 days later; auto-archive on PR merge/close | D § Archive a session | Delete UI **not documented** for Desktop (documented for web) |
| Rename | Click title; Claude can rename via cross-session tools | D | |
| Cross-session work | Claude lists (20 most recent), reads, messages, renames, archives other Desktop sessions; approval card for archive except Auto/Bypass; `crossSessionInbound` | D § Work across sessions | sees only Desktop-hosted local/SSH/WSL sessions |
| Cloud sessions | **Cloud** environment; multi-repo; continue after app closes; monitor at claude.ai/code + mobile | D § Run long-running tasks in the cloud | |
| Open in → Cloud | Move a local session to cloud (conversation carried as summary; dialog says if files move / original archived) | D § Continue in another surface | not SSH/WSL |
| Teleport | Cloud → terminal via `claude --teleport`, `/teleport`; claude.ai **Open in > Terminal** | WEB § Continue a cloud session in your terminal | CLI-side; Desktop entry point **not documented** |
| SSH sessions | Auto-installs Claude Code remotely; `~/.ssh/config` (ProxyJump etc.); keeps running while Desktop disconnected (v2.31226.0+); host-key verification UI; Windows remote hosts (less tested) | D § SSH sessions | Browser pane unavailable over SSH |
| WSL sessions | Run in WSL 2 distro with native Linux paths | https://code.claude.com/docs/en/desktop-wsl | Windows; no plugins; terminal/connectors may be missing |
| Scheduled tasks | Local tasks (Hourly/Daily/Weekdays/Weekly/Manual/Custom cron); one catch-up run after sleep; per-task permission mode + Always-allowed list; **Run now**, Active/Paused, History; cloud scheduled tasks (formerly Routines) on same page | DST | local only while app open & awake |
| Dispatch | Message a task from phone (Cowork); Dispatch may spawn a Code session that appears in sidebar | D § Sessions from Dispatch | Pro/Max/Ultra only |
| Suggested tasks | Claude-proposed side tasks → new sessions | D | |

## 8. Code review & dev surfaces

| Item | Description | Source | Status |
|---|---|---|---|
| Diff view | File-by-file; opened from **Changes**; Cmd/Ctrl+Shift+D | D § Review changes with diff view | |
| Inline comments | Hover line → **+** → comment; Enter adds; Cmd/Ctrl+Enter submits all; Claude revises → new diff | D | |
| Compare against branch | **Compare against** any branch | WEB § Review changes | documented for cloud |
| `/code-review` | Findings card; **Walk through in diff**, **Fix this one**, dismiss, **Apply fixes** | D § Review your code | |
| PR creation | **Create PR** (uses `gh`; prompts to install if missing) | D § Monitor pull request status | SSH: set up gh remotely |
| PR + CI status | PR in **Changes** card with CI checks; **⋮ → Auto-fix** (CI failures + trusted reviewers' comments), **Merge when ready** (GitHub auto-merge; squash unless merge queue) | D | Merge when ready disabled in SSH/WSL; earlier layout: **CI** button |
| Preview browser | Tabbed **Browser** pane; dev servers auto-start; **Dev servers** menu; auto-verify (screenshots, DOM, clicks, forms, logs); element select Cmd/Ctrl+Shift+S; OAuth popups; safety classifiers + allowlist on external sites | D § Preview your app, § Browse external sites | not SSH |
| Terminal | Integrated, session cwd + same env; multiple tabs; **Open in terminal** from folder; Ctrl+` | D § Run commands in the terminal | local + SSH (not Windows-path SSH folders) |
| File viewer/editor | File pane: editable in local sessions with **Auto save**/Save/Cancel and Override/Discard conflicts; view-only in cloud/SSH/WSL | D § Open and edit files | |
| Open in editor | VS Code / Cursor / Zed / file manager | D | |
| iOS Simulator pane | Live-streamed simulator; Claude taps/types/screenshots | https://code.claude.com/docs/en/desktop-ios-simulator | **public beta**, macOS only, v1.24012.0+, needs Xcode |
| Computer use | App/screen control; tiers View only / Click only / Full control | D § Let Claude use your computer | **research preview**; macOS 14+ & Windows; Pro/Max/Ultra only; not Linux |

## 9. Keyboard shortcuts & accessibility

Source: D § Keyboard shortcuts (macOS keys shown; Windows/Linux: Cmd→Ctrl, Cmd+Option→Alt, Cmd+Ctrl→Ctrl+Alt; Ctrl-prefixed stay).

| Shortcut | Action |
|---|---|
| Cmd / | Show all shortcuts |
| Cmd N / Cmd W | New / close session |
| Cmd Opt 1–9 / Cmd Opt 0 | Select environment / New environment dialog (new-session screen, current layout) |
| Ctrl Tab / Ctrl Shift Tab; Cmd Shift ] / [ | Switch sessions; next/previous |
| Esc | Stop response |
| Tab / → | Accept suggested prompt |
| Cmd Shift D / B / S | Diff pane / Browser pane / select element in Browser |
| Ctrl ` | Terminal |
| Cmd \ | Close focused pane |
| Cmd Ctrl W | Leave split view |
| Cmd ; | Side chat |
| Ctrl O | Cycle view modes |
| Cmd Shift M / I / E | Permission-mode / model / effort menu |
| 1–9 | Pick item in open menu |
| Cmd Enter | Submit all diff comments |

Not available: Shift+Tab mode cycling (D). Custom keybindings (`keybindings.json`) — **not documented** for Desktop.
Accessibility (screen reader, contrast, theme, font scaling): **not documented** in Claude Code docs.

## 10. Remote / mobile / multi-device

| Item | Description | Source | Status |
|---|---|---|---|
| Remote Control from Desktop | Per-session **Remote Control** switch (Environment card) or `/remote-control`; **Connect new sessions to Remote Control**; `remoteControlAtStartup`, `disableRemoteControl` | D § Control which sessions appear on your other devices | claude.ai login, api.anthropic.com only; Team/Ent admin toggle |
| What remote devices get | Live conversation, subagent/workflow progress sync, attachments (photos + files → `@` refs), diff pane, model/effort control, permission prompts and AskUserQuestion kept open until answered; other forwarded dialogs expire after 5 min (`dialogExpiry`) | RC § What connected devices see, § Limitations | |
| Remote modes | RC sessions: Manual/Accept edits/Plan/Auto (no Bypass from app) | PM; MOB | |
| Local-only commands | `/plugin`, `/resume` etc. don't work remotely; picker commands take args (`/model sonnet`) | RC § Limitations | |
| Archive sync | Archiving in Desktop archives the RC copy too | D | |
| claude.ai/code (web) | Cloud sessions: repo/branch picker, environments, diff + comments + compare, PR creation, Auto-fix, sharing (Private/Team or Public), archive/delete, queued-message take-back, **Rewind to here** | WEB | Pro/Max/Ultra/Team, Ent premium seats; GitHub required for clone/PR |
| Mobile apps | iOS (incl. iPad) / Android **Code** tab: cloud sessions, Projects, Remote Control, Dispatch; push notifications | MOB | no Bypass; local-only commands unavailable |
| Dispatch | Phone → Cowork → Desktop Code session; push on finish/needs approval | D § Sessions from Dispatch | Pro/Max/Ultra only |
| Trusted Devices | Org can require enrolled devices for RC | RC § Trusted Devices | beta |
| Channels / Slack | Telegram/Discord/iMessage channels (CLI); `@Claude` in Slack (cloud) | RC comparison table; MOB | |

---

## CLI (terminal TUI) features Desktop lacks or does differently

Official "What's not available in Desktop" + comparison table (D § Coming from the CLI):
- **Third-party providers** (Bedrock, Agent Platform, Foundry) — Desktop is Anthropic API by default; gateway routing / "Claude Desktop on 3P" separate.
- **`dontAsk` permission mode** — CLI only. **Shift+Tab** mode cycling — CLI only.
- **Agent teams** — CLI only (Desktop: dynamic workflows + cross-session messaging instead).
- **Inline code suggestions** (autocomplete completions) — not in Desktop.
- **Terminal-dialog commands** (`/permissions` etc.) → `isn't available in this environment`; `/config` opens Settings.
- **Scripting**: `--print`, `--output-format`, Agent SDK — "Desktop is interactive only."
- **`--allowedTools` / `--disallowedTools`** — no per-session equivalent.
- **Computer use on Linux** — not in the Linux beta.

Desktop-only (absent in CLI): file attachments (images/PDFs), Connectors UI, plugin manager UI, Browser pane, iOS Simulator pane, Dispatch, scheduled-task UI, split view, diff inline comments, PR monitoring card.

CLI features documented for the terminal with **no Desktop documentation** (treat as absent/unknown, not confirmed absent):
- `/rewind` / Esc Esc checkpoint menu (https://code.claude.com/docs/en/checkpointing)
- Plan-approval menu options and Ctrl+G plan editing (PM § Review and approve a plan)
- Voice dictation `/voice` (https://code.claude.com/docs/en/voice-dictation)
- Vim mode, Ctrl+R reverse history search, `!` shell mode, transcript viewer, themes, statusline, spell check (https://code.claude.com/docs/en/interactive-mode)
- Task list rendering (interactive-mode § Task list)
- Critical-path `rm` countdown prompt (Desktop denies instead — PM)

## Recent changelog items touching the Code tab (CL)

- v2.1.316: `/code-review` findings card now in cloud + RC sessions started from the Code tab.
- v2.1.314: idle compaction in Desktop's Code tab and VS Code.
- v2.1.302: subagents of the main conversation always run in background in Desktop; fork subagents on by default.
- v2.1.292: fix for **Send now** on queued messages in the desktop app.
- v2.1.295: permission-prompt status naming MCP tools by readable name in Desktop / claude.ai / RC.

## Secondary sources (gap-fill only, unverified)

- **[secondary]** VentureBeat reports a full redesign of the Claude Code desktop app on 2026-04-14 (parallel sessions, embedded terminal) alongside the Routines research preview: https://venturebeat.com/orchestration/we-tested-anthropics-redesigned-claude-code-desktop-app-and-routines-heres-what-enterprises-should-know — consistent with the "current layout" vs "earlier layout" split in D.
- **[secondary]** A third-party site dates the Windows build of Claude Code Desktop to 2026-02-10: https://claude-for-operators.pages.dev/platform/code/
- Official support release notes (https://support.claude.com/en/articles/12138966-release-notes) could not be fetched directly.
