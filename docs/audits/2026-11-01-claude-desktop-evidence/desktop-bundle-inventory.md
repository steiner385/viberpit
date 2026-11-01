# Claude Desktop "Claude Code" mode (ccd / "epitaxy") — bundle inventory

Host: dobby. Package `claude-desktop` 4.4212.0 (`app.asar` package.json `@ant/desktop` version 4.4212.0), renderer bundle `ion-dist.asar`.
Survey date 2026-11-01. Read-only: I extracted both asars with a pure-Python reader into `desktop-asar/app/` and `desktop-asar/ion/`
under this scratch dir, and executed nothing from them. I did not touch the running app or its config. I did not read
`buddy-tokens.json`, Cookies, Local State or any token values.

## Sources and how to read the evidence tags

| Source | What it is |
|---|---|
| `app/.vite/build/mainView.js` + `index.chunk-*.js` | Main process and preload. IPC channels are `$eipc_message$_<uuid>_$_claude.web_$_<Interface>_$_<method>`. I enumerated 1,414 unique `Interface_$_method` channels (list at `/tmp/desk-ipc.txt`). |
| `ion/` (`ion-dist.asar`, 195 MB) | The renderer is bundled locally, not served from claude.ai's cache: `index.html` plus 4,480 chunks in `ion/assets/v1/`. |
| `ion/i18n/en-US.json` (3 MB) | UI string catalogue keyed by hashed id. **Caveat:** it covers all of Claude Desktop (Chat, Cowork, Admin, Billing, Code), so I only attribute a string to Code mode when its wording or co-location ties it to Code. |
| `~/.config/Claude/` | User data. I recorded keys, types and enums only. |

Internal codenames seen: **ccd** = Claude Code Desktop; **epitaxy** = the Code UI (`sidebarMode: "epitaxy"`, `epitaxyPrefs`, `EpitaxyPrBar`,
`data-testid="epitaxy-split-suggestion"`); **dframe** = desktop frame / pane layout; **LocalSessions** = the Code session IPC
(355 methods); **LocalAgentModeSessions** = Cowork.

Engine: the main process spawns the **real Claude Code CLI**. It downloads the CLI per version into `~/.config/Claude/claude-code/<ver>/`
(2.1.299 and 2.1.308 are present) and runs it with `--input-format stream-json --output-format stream-json --include-partial-messages
--permission-prompt-tool … --resume-session-at --fork-session --include-hook-events --max-thinking-tokens --effort --fallback-model
--permission-mode --allow-dangerously-skip-permissions --add-dir --plugin-dir --mcp-config --managed-settings --no-session-persistence …`.
It talks to the CLI over the SDK control protocol (`control_request`/`control_response`). Subtypes found: `initialize, set_model,
set_permission_mode, set_max_thinking_tokens, set_cwd, rewind_files, rename_session, stop_task, get_task_output, start_shell_task,
side_question, turn_preempted, mcp_toggle, mcp_status, mcp_set_servers, mcp_reconnect, mcp_read_resource, reload_skills, reload_plugins,
reload_output_styles, update_settings, remote_control, ultrareview_launch, set_prompt_suggestions_paused, permission_request,
permission_response, permission_auto_approved/denied, message_rated, submit_feedback, ui_* (EngineSurface)`. The main process also calls
`query.getContextUsage()` (SDK-style). VERIFIED-from-bundle.

---

## 1. App shell & information architecture

- **Left sidebar** with collapse and resize. Labels: "Toggle sidebar", "Resize sidebar", "Edit sidebar…" ("Choose which items appear in
  your sidebar"), "Next/Previous sidebar tab". Boot state stores `sidebarWidth: 288`, `collapsed`, `narrowViewportMaxWidth: 700`.
  VERIFIED-from-bundle (strings) + VERIFIED-from-userdata (`config.json.bootFrameLayout`).
- **Sidebar sections** are typed and user-configurable: `dframe-code-sections[acct/org].sections[]`, each with
  `{id, kind, name, order, shown, collapsed, sortBy, ascending, show:{metadata, emptyGroups, sideChats}, v}`. The kinds present are
  `pinned`, `routines` and `sessions`, with `sortBy: "recency"`. VERIFIED-from-userdata.
- **Grouping:** "Group by" options include Project, Folder, Branch, Status, Environment, Date and None. "Group by type", "Create and group
  by folder/custom", "Group sessions by PR state from the filter icon at the top of the sidebar", "Sort sessions from the filter icon". IPC
  `LocalSessions.setCustomGroups`. Filter state lives in `epitaxyPrefs.ccd-sessions-filter.state.selectedProjects`. VERIFIED-from-bundle +
  VERIFIED-from-userdata.
- **Pinning / starring:** prefs keys `starred-local-code-sessions`, `starred-session-groups`, `starred-local-routines` and
  `dframe-local-slice.pinnedOrder` / `homeProjectsPinnedOrder`. Strings: "Pinned {name} to the sidebar", "Sidebar pins and starred sessions
  survive sign-out". VERIFIED-from-userdata + bundle.
- **Panes / split view:** a multi-pane tiling layout. The pane store `desktop-frame.paneStore.v1` holds `extraPanesByMode`,
  `colWeightsByMode`, `rowSplit` and `lastPrimaryCodeSession{id,type}`. Strings: "Split view", "Open in split view", "Primary pane",
  "Secondary pane", "Resize pane columns/rows", "Move split view up/down", "Focus next/previous split view", "Expand pane", "Close pane".
  There is a "See multiple sessions side by side in split view" suggestion toast (`epitaxy-split-suggestion` with shown/dismiss counters).
  IPC `MenuEvents.splitViewCommand`, `WindowLayout.layoutCommand`, `LocalSessions.setSessionPaneMenuState`. VERIFIED.
- **Side pane types** (keybinding commands): preview, diff ("Changes"), terminal, browser/files ("Files pane"), side chat, background
  tasks. VERIFIED-from-bundle (keybinding registry, §9).
- **Pop-out / tear-off windows:** "Open a session in its own window … drag it out from the sidebar, or cmd+click it". IPC
  `SessionPopouts.reportOpenPopouts/takeRestorablePopouts/takeRestorableLayout` (layout restore across launches) and
  `LocalSessions.beginTearOffHalo/setTearOffHalo/endTearOffHalo` (drag-to-tear-off visual). VERIFIED.
- **Command palette** (Cmd/Ctrl+K: "Search or start a session"; "finds sessions you started since opening the app, and searches branches
  inside folders") and a separate "Search" palette. IPC `MenuEvents.openCommandPalette`, `LocalSessions.searchSessions`. VERIFIED.
- **Title bar:** `MainWindowTitleBar` IPC (8 methods). Remote Control shows as a title-bar badge ("click it to turn on or off").
  VERIFIED-from-bundle.
- **Session read state:** "Mark session as read/unread" (shortcut Cmd+Alt+U). VERIFIED.
- **Window state persisted:** `window-state.json {x,y,width,height,isMaximized,isFullScreen,displayBounds}`. VERIFIED-from-userdata.

## 2. Chat transcript rendering

- **Three transcript views:** Normal, Verbose and Thinking ("Switched to Thinking view"; "See a one-line recap of Claude's thinking above
  each tool group in thinking view"). Cycle with Ctrl+O (`cycleTranscriptMode`). A "Default transcript view" setting can be overridden per
  session. VERIFIED-from-bundle.
- **Thinking blocks:** "Thinking…", "Show full thinking", "Toggle thinking". IPC `LocalSessions.setThinkingSummariesWanted`. VERIFIED.
- **Tool calls:** "{count} tool uses", "Tool call details", "Tool call input"; tool groups are collapsible (INFERRED from "above each tool
  group"). Background shell tasks: "Run in background", "Background tasks" panel (Ctrl+B), `getTaskOutput`, `stopTask`,
  `interruptBashCommand`. VERIFIED.
- **Subagents:** "Running subagent", "Report from subagent", "Message from subagent", "Sent by a subagent". The session field
  `subagentsTruncatedFor` suggests a long subagent list gets truncated. VERIFIED / INFERRED.
- **Todos:** "Updated todos", "Cleared todos", "Regarding Todo item: “{content}”". VERIFIED.
- **Compaction marker:** "`/compact` now shows a marker in the transcript where context was compacted"; "Compacted conversation". The
  control subtype `compact_boundary` is handled. VERIFIED.
- **Hook feedback inline:** "A UserPromptSubmit hook stopped this prompt…", "Hook re-prompted Claude", "Refused by a hook". VERIFIED.
- **Message actions:** "Copy message as Markdown", "Attach message as context", "Attach selection as context" (Cmd+Shift+L), "Fork from
  here", "Rewind to here", "Fork with this prompt", "{n} forks from here". VERIFIED.
- **Long transcripts are paged:** IPC `getTranscriptPage`, `getTranscriptTail`, `reportTranscriptLoadEarlier`,
  `reportTranscriptWindowBackfill`, `requestTranscriptPort` (a MessagePort stream), and a `transcript-search-worker` in the main process.
  VERIFIED.
- **Images and media:** `getSessionImageStreamUrl`, `readSessionImageAsDataUrl`, `getSessionMediaStreamUrl`, `fetchGithubImage`, and
  `highlightWorker` for syntax highlighting. VERIFIED.
- **File references:** "Open in Files pane", "Reveal in file tree", "Transcript links in preview" (pref
  `epitaxy-transcript-links-in-preview` plus a "chooser seen" flag). Links can route to the in-app preview or externally (IPC
  `Launch.openClickedLinkInPreview`, `setPreviewLinkRouting`). VERIFIED.
- **Post-turn summary / turn wrap-up card:** session fields `postTurnSummary{status_category ∈ completed|review_ready…, status_detail,
  needs_action}`, `turnWrapUp{description}`, `lastTurnReport` and `reportFindingsCard`. VERIFIED-from-userdata.
- **Streaming:** `--include-partial-messages` and `reportStreamRender` telemetry. VERIFIED.

## 3. Composer

- **Keys:** Enter sends, Shift+Enter adds a newline, Cmd+Enter / Cmd+Alt+Enter queues "for later (while Claude is working)", plus "Send
  and stay on the new session page" and "Send in a forked session (local sessions)". VERIFIED (shortcut dialog).
- **Message queue:** "Queued · sends when Claude is ready", "Queued messages collapse into a stack — click to expand", reorder, remove,
  "Send now". IPC `cancelQueuedMessage`, `promoteQueuedMessage`, `reorderQueuedMessage`, `sendSteeredNow` (mid-turn steering) and
  `discardPendingTurn`. VERIFIED.
- **Stop / interrupt:** Esc stops Claude's response. Double-Esc rewinds to an earlier message or edits your last message. IPC
  `interrupt`, `stop`. VERIFIED.
- **Prompt history:** "Reuse an earlier prompt" (↑) and "Search earlier prompts" (Ctrl+R). Jump to the previous or next prompt in the
  transcript with Cmd+Alt+↑/↓ (Alt+↑/↓ off macOS). VERIFIED.
- **Pickers:**
  - Mode menu: Cmd+Shift+M, or Cmd+Alt+M.
  - Model menu: Cmd+Shift+I.
  - Effort selector: Cmd+Shift+E. Levels low/medium/high/max; "Default model effort".
  - Fast mode toggle: Cmd+Alt+F.
  - Output style is chosen per session.

  The UI warns "Change effort?" and "Change thinking mode?" (prompt-cache cost; pref `modelSwitchCacheWarningSkip`). VERIFIED.
- **Attachments:** "Attach file" (Cmd+U), "Drop files or a folder here", paste images (`FileSystem.savePastedFile`). File
  @-mentions are supported: "A command takes file @-mentions but no other @-mentions…". VERIFIED.
- **Slash-command autocomplete:** a merged desktop + CLI list (§4). `argumentHint` is shown. VERIFIED.
- **Prompt suggestions:** "Show a suggested next prompt in the message box after Claude replies" (a setting).
  `set_prompt_suggestions_paused` exists. VERIFIED.
- **Dictation:** IPC `Dictation.getStreamCredential` (server-streamed STT). Strings: "Press once to start dictation…", "Dictation is
  still finishing, so your message wasn't sent…". VERIFIED.
- **Presets ("Environments"):** in the UI these appear as "Select environment" / "New environment", with Cmd+Alt+1…9 / Alt+1…9 to
  pick one and Cmd+Alt+0 for new (gate `sessionPresets`). "You often start on {place}; save it as an environment to start here in one
  click". IPC `LocalSessionPresets.load/replaceAll`. VERIFIED.

## 4. Commands & configuration

- **Desktop-native slash-command registry** (renderer chunk `cc183b3ab-*.js`, objects `{name, aliases, argumentHint, gate, cliHandled,
  neverReachesCli, hidden, terminalAction}`): `ultrareview, rewind, fork [prompt], subtask <task>, plugin (plugins, marketplace),
  model [id], effort [level], fast [on|off], plan [open|<description>], permissions (allowed-tools), skills, output-style, rename (name),
  export, add-dir, cd, diff, tasks (bashes), workflows, status, usage (stats, cost), stats, usage-credits (extra-usage), context,
  autocompact [auto|<tokens>], memory, storage, sandbox, theme, logs, advisor, login (logout), help, copy [N], clear (reset, new),
  compact, btw, feedback, mcp, config, resume, auto-mode-setup, reset-limits*, create-pr*` (* hidden). VERIFIED-from-bundle.
- **Known CLI passthrough set:** `add-dir, agents, allowed-tools, autocompact, bashes, branch, chrome, copy, desktop, diff, doctor,
  effort, exit, fast, goal, help, hooks, ide, import, install-github-app, install-slack-app, keybindings, login, logout, loop,
  marketplace, memory, mobile, permissions, plugin(s), privacy-settings, release-notes, reload-plugins, reload-skills, remote-env,
  security-review, session, skills, status, statusline, tasks, teleport, terminal-setup, theme, ultraplan, upgrade, vim, voice` and more.
  Anything unknown is classed `custom`, and `mcp__*` as `mcp`. The CLI's own list comes over IPC `getSupportedCommands`. VERIFIED.
- **Skills:** a Skills browser plus `LocalPlugins.listSkillFiles`, `reload_skills`. Bundled skills: docx, pdf, pptx, xlsx,
  frontend-design, pdf-reading (`app/resources/bundled-skills/`). VERIFIED.
- **Plugins / marketplaces:** IPC `CustomPlugins.*`: add/remove/refresh marketplace, list/install/update/uninstall plugin, local org
  plugins. `LocalPlugins.*`: enable, env vars, options, OAuth, upload. VERIFIED.
- **MCP:** global and per-session MCP config. IPC `MCP.getMcpServersConfigWithStatus/revealLogs/revealServerLog` and
  `LocalSessions.mcpAuthenticate/mcpReconnect/mcpEnableServer/mcpCallTool/replaceEnabledMcpTools/setMcpServers`.
  - Per-session tool toggles: `enabledMcpTools` map and a "Session MCP dialog".
  - Per-account tool toggles: `mcp-user-tool-toggles.json {v:3, owners:{<acct>:[]}}`.
  - SSH-host tool grants: "Allowed connector tools".

  VERIFIED.
- **Hooks:** Claude Code hook events are labelled "Pre-tool use", "Post-tool use", "Subagent stop", "Pre-compact" and "Runs before/after a
  tool call". There is a WorktreeCreate hook ("Running your WorktreeCreate hook…"). The session field `spawnSeed.worktreeHookBased`
  exists. I found no evidence of a hooks *editor* for Code; the hooks strings sit in plugin/skill detail views. VERIFIED (labels);
  editor not found.
- **CLAUDE.md / memory:** "Memory files", "Memory file tree", "Edit CLAUDE.md files", "Project CLAUDE.md", "Delete memory file" (the
  `/memory` dialog). VERIFIED.
- **Output styles:** create (`createOutputStyle` with an AI-generated description), list, reveal, set per session or default.
  VERIFIED.
- **settings.json:** IPC `ClaudeCode.patchUserSettings`, `resolveLocalSettings`, `resolvedSettingsChanged`,
  `LocalSessions.applyFlagSettings` and `requestUserSettingsRead`. "Managed by your organization" badges follow managed settings.
  VERIFIED.
- **Output of `promptAppendSnapshot{append, cliVersion, settingsKey, toolSets}`** per session: the desktop appends its own system-prompt
  text. VERIFIED-from-userdata.

## 5. Settings surfaces

Top-level Settings sections, taken from in-string breadcrumbs: General, Account, Appearance, Notifications, Privacy, Billing, Usage,
Capabilities, Connectors, Skills, Plugins, Cowork, **Claude Code**, Developer (Edit config), This computer (Computer use), Import,
Environment, Webhooks. VERIFIED-from-bundle.

**Settings → Claude Code** labels (settings search index, chunks `c71860c77`, `c8b630735`, `ca25db325`):
- **Pull requests:** "Auto-fix pull requests" ("Claude automatically monitors it for CI failures and review comments, then responds
  proactively"), "Auto-archive after PR merge (or close)", "Open pull requests in browser", "Branch prefix" (1–40 chars, applies to local
  and cloud).
- **Worktree:** "Worktree location" ("Where to store Git worktrees…"). The landing worktree toggle is pref `cc-landing-worktree-enabled`.
- **Sandbox:** "Local sandbox" ("Runs commands from Claude Code in an isolated sandbox"), "Strict sandbox mode". IPC
  `ClaudeCode.setLocalSandboxEnabled/setStrictSandboxMode`.
- **Prompt suggestions**, **Classify session states** ("automatically classify sessions as blocked, ready for review, or done…counts
  towards your plan usage"), **Output style** default, **Default transcript view**, **Code appearance** (file icon theme), "Transcript
  text size/width".
- **Preview / Browser:** "Auto-open files Claude writes", "Open links in built-in browser", "Keep cookies", "Allowed sites", "Browser
  tools".
- **SSH sessions** (manage connections), **Allowed connector tools**, **Remote Control** ("Turn on Remote Control for all new local
  sessions"), **Cloud environments** (Name, Access, Env vars, Setup script), **Mobile simulators** (iOS Simulator, Android Emulator),
  **Keep computer awake**, "Environment variables apply to every local session".
- **Notifications:** "Code notifications", "Code permission requests", "Questions", "Response completions", "Scheduled tasks",
  "Dispatch messages". Levels: Off / Badge only / Banners. Dock-bounce / taskbar-flash attention.
- **Permissions:** default permission mode (IPC `getDefaultPermissionMode`), "Allow bypass permissions mode" (pref
  `bypassPermissionsGateByAccount`), "Make auto mode your default permission mode?". `ClaudeCode.setEnableWorkflows`.
- **Global shortcut** (`GlobalShortcut.get/set`), install `claude` CLI shim (`CcdCli.install/uninstall`).

VERIFIED-from-bundle; prefs VERIFIED-from-userdata (`claude_desktop_config.json.preferences`: `ccdScheduledTasksEnabled`, `sidebarMode`,
`bypassPermissionsGateByAccount`, `remoteSessionFolderGrants`, `launchPreview*`, `epitaxyPrefs`).

## 6. User feedback

- **Permission modes:** Ask permissions (`default`), Accept edits, Plan mode, Auto mode (classifier-gated: "Claude will decide which
  actions are safe…", "Blocked by auto mode: {reason}"), Bypass permissions. The values seen in session files are `default`, `auto` and
  `bypassPermissions`. VERIFIED.
- **Permission card buttons:** "Allow once", "Allow for this session", "Always allow" ("switches this session to Accept edits…"), and
  "Deny" (Cmd+.). "More accept options" (Cmd+Shift+Enter). Session fields `alwaysAllowedReasons` and
  `sessionPermissionUpdates[{type, destination, directories}]`. IPC `onToolPermissionRequest` / `respondToToolPermission`. VERIFIED.
- **Plan approval:** "Plan ready for review", "Plan approval", "Claude wants to leave plan mode", "You can comment on the plan in plan
  mode, or when Claude asks you to approve it". `/plan open` shows the current plan. VERIFIED.
- **AskUserQuestion:** "Waiting for your answer", "Awaiting answer", "Claude needs your input", "Needs your input. Scroll to the request".
  Notification category "Questions". VERIFIED (the exact multiple-choice widget strings were not isolated).
- **Workspace trust:** "Trust workspace", "Trust repository". IPC `checkTrust/saveTrust/answerPendingCwdTrust`. VERIFIED.
- **Status indicators:** "Needs input", "Waiting for you · {when}", "Awaiting review", plus classifier states blocked / ready for review /
  done (`postTurnSummary`). IPC `SessionStatusText.read`. VERIFIED.
- **Notifications:** desktop notifications with actions (`DesktopNotifications.showNotificationWithActions`,
  `setProjectPromptsWaiting`), badge, Dock/taskbar attention, toasts (`Toast.showToast`), and mobile push via Dispatch. VERIFIED.
- **Usage / limits:**
  - Plan usage history is sampled to `plan-usage-history.json {version:2, samples[{t, org, u:{fh, sd}}]}`, where `fh`/`sd` are likely the
    five-hour and seven-day percentages (INFERRED).
  - IPC `PlanUsage.nudgeRefresh`, `ClaudeCode.getPeriodUsage`, `getCodeStats`.
  - "Approaching usage limit", "Resets at {time}".
  - **"Auto-continue when limits reset"** (`LimitAutoResume.setLimitAutoResumeArmed/HeldSessions`).

  VERIFIED.
- **Context meter:** `getContextUsage` / `getContextUsageSummary`; "{percent}% used / left", "Auto-compacts soon", "Why compact now", and a
  `/context` popover. VERIFIED.
- **Errors / recovery:** "Switch model and retry", "Lower effort and retry", "Rewind and retry", "More recovery options", "Get logs and
  open Slack" (`createLogsHandoffBundle`), plus SSH/WSL remedies (`runSSHRemedy`). VERIFIED.

## 7. Session lifecycle & branching

- **New session:** choose a folder (Cmd+Shift+O Open Folder), an environment preset and a worktree on/off ("Start with worktree…", "Start
  worktree from <branch>"). IPC `start`, `prewarmLocal/prewarmFolderless`, `warmSession`, `createScratchWorkspace` (folderless scratch
  workspaces live in `scratch-workspaces/`). VERIFIED.
- **Session record** (`claude-code-sessions/<accountUuid>/<orgUuid>/local_<uuid>.json`) has these keys:
  - Identity, location and timing: `sessionId, cliSessionId, cwd, originCwd, gitAnchorsFolderRealpath, createdAt, lastActivityAt,
    lastFocusedAt, lastTurnEdgeAt`.
  - Engine settings: `model, effort, permissionMode, chromePermissionMode`.
  - Title: `title, titleSource ∈ auto|tool|user, previousTitles, titleTurn`.
  - Lifecycle: `isArchived, completedTurns`.
  - Git: `branch, sourceBranch, worktreeName, worktreePath, writtenBranches[], prs[{repo, host, prNumber, branch, baseRef, state, url}]`.
  - Lineage: `forkedFromSessionId, spawnedFrom{sessionId, name, title, taskId, sideSession, btw, userFork, sharedFolder}`,
    `btwLoweredFrom`.
  - Side sessions and peers: `sideSessionNotes[{messageUuid, sessionId, title, report, at, batch}]`,
    `peerReceipts[{messageId, targetSessionId, sentAt, deliveredAt, readAt, delivery, toolUseId}]`, `peerMessaged`.
  - Remote: `bridgeSessionIds, remoteControlUserEnabled, remoteControlAutoEligible, steeredByRemoteClient`.
  - Errors: `error, errorAt, priorErrorMark`.
  - Snapshots: `toolSurfaceSnapshot, promptAppendSnapshot`.

  Deleted sessions leave tombstones named `deleted_<uuid>`. VERIFIED-from-userdata.
- **Worktrees:** create, enter, leave, move-to-worktree, "Delete worktree", "Discard kept worktree", "Remove unused worktrees",
  "Worktree needs repair", and garbage collection of untracked worktree dirs (`git-worktrees.json {schemaVersion:2, worktrees,
  untrackedDirGc{sightings, roots, cwds}}`). IPC `checkBranchSwitchConflict`, `commitWipForBranchSwitch`, `stashWorkingTree`,
  `discardWorkingTree`, `SourceBranchPrefetch`. VERIFIED.
- **Resume / import CLI sessions:** `listCliSessions`, `listResumableCliSessions`, `previewCliResume`, `resumeCliSession`,
  `importAllCliSessions`, `CliSessionImport.*` (CLI sessions show up in the sidebar), `openCliTail` (live-tail a terminal CLI session),
  and `resumePreClearSession`. VERIFIED.
- **Fork:** "Fork session" (Cmd+Alt+O), "Fork from here", "Fork with this prompt", `/fork [prompt]`, `forkSession` (`--fork-session`).
  VERIFIED.
- **Rewind / checkpoints:** "Rewind to here", "Undo rewind", "Redo rewind", "Rewind and retry". IPC `rewind`, `rewindV2`, `rewindFiles`
  (code restore via `rewind_files`), `undoRewind`, `switchRewindMark`, `rewindToRefusedPrompt`. A `git-shadow/` dir holds shadow git
  repos (INFERRED: file checkpoints). VERIFIED.
- **Edit-and-resend:** "Edit your last message" (double-Esc), "Send or clear your draft first, then select Rewind and retry". VERIFIED.
- **Side chats (/btw):** Cmd+; opens them. They are tabbed, pinnable, and can float in their own window ("remembers where you placed
  and sized it"). IPC `startSideChat/sendSideChatMessage/stopSideChat/startBtwChild/closeBtwChild` (control `side_question`). There are
  also **side-session offers**, where Claude proposes spawning a parallel task (`acceptSideSessionOffer/muteSideSessionOffers`,
  `popBackgroundTaskSuggestion`, "Suggested tasks"). **Side-session notes** are markdown files written to
  `side-session-notes/<sessionId>/`. VERIFIED.
- **Subtask / dispatch:** `/subtask <task>`; `notifySpawnedTaskStarted` (parent→child sessions); `getTaskBacklog`. VERIFIED.
- **Archive:** "Archive session" (Cmd+Alt+A / Cmd+Shift+Backspace), "Archive all", "Show archived", "Unarchive", auto-archive on PR merge,
  and "archived automatically when its background task finishes". `loadArchivedSessions`. VERIFIED.
- **Rename:** Cmd+Alt+R, `/rename`, and AI title offers (`respondToTitleOffer`). VERIFIED.
- **Remote / cloud / SSH:**
  - **Move to cloud** (teleport): `getTeleportReadiness/getTeleportPlan/teleportToCloud`, "Continue in cloud" (pushes the branch or
    uploads a snapshot).
  - **SSH sessions:** the full SSH stack (`getSSHConfigs`, `testSSHConnection`, password prompts, host key, worktree location per host,
    remote git and diff, `reinstallRemoteClaudeCode`).
  - **WSL:** `listWslDistros`, `canStartWslSession`.

  VERIFIED.
- **Scheduled tasks / Routines:** IPC `CCDScheduledTasks.*` (create, update, status, watcher history, dispatch claim). Per-org
  `scheduled-tasks.json {scheduledTasks[], recordedSkips, sundayAliasBoundaryStamped}`. "Routines" sidebar section; local or cloud runs;
  "Scheduled task “{name}” missed at {time}. Running now." VERIFIED.
- **Keep awake per session:** `setManualKeepAwake`. VERIFIED.

## 8. Code review & dev surfaces

- **Diff viewer ("Changes" pane):** Cmd+Shift+D toggles it; file list Cmd+Shift+Y; next/prev change with F7/Shift+F7; "Go to file" with
  Cmd+P; layout Unified / Side by side, Word wrap, Show/Hide whitespace changes; "Find in changes"; "Add comment on this line" (comments
  go back to Claude). IPC `getGitDiff`, `getGitDiffStats`, `getGitDiffFilePatch`, `getDiffFileContent`, `getDiffFileImage`,
  `getCommitDiff`, `getGitCommits`, `getUncommittedChanges(ByFolder)`. VERIFIED.
- **Files pane:** fuzzy-find by name, content search with `?`, Cmd+F in a file, Cmd+S to save (editable). "File tree position". IPC
  `listSessionDirectory`, `readSessionFile`, `writeSessionFile`, `openInEditor` (`getInstalledEditors`). VERIFIED.
- **PR flow:**
  - Create (draft) PR via `/create-pr` or "Create PR".
  - An in-app **PR viewer** covers overview, files, commits, timeline, checks with annotations, rerun check, review comments with reply
    and resolve, submit review, edit body/comment, mark ready/draft, merge, and enable/disable auto-merge. A merge queue is also present
    ("Added to merge queue", `dequeuePr`). IPC `getPrOverview/getPrFiles/getPrChecks/getCheckRunAnnotations/rerunPrCheck/
    replyToPrReviewComment/resolvePrReviewThread/submitPrReview/mergePr/enableAutoMerge/markPrReady/markPrDraft`.
  - "Open session PR" (Cmd+Alt+G); next/prev unresolved PR comment (Alt+F9).
  - **Auto-fix PRs** (CI and review-comment babysitting); `EpitaxyPrBar`.
  - **Ultrareview** (`launchUltrareview`, "Run ultrareview"); `reviewRepository`.
  - gh install check (`checkGhAvailable`, `installGh`). Issues: `listGhIssues`, `getGhIssue`.

  VERIFIED.
- **Preview / built-in browser:** IPC `Launch.*` (123 methods): detect dev-server candidates (`detectServerCandidates[WithAgent]`),
  `startFromConfig` (from `.claude/launch.json`-style configured services — INFERRED), preview tabs (Cmd+T new, reopen, Ctrl+Tab),
  address bar (Cmd+L), viewport presets, color scheme, element **selection mode** (Cmd+Shift+S, sends the element as context), **sketch /
  annotate mode** (Cmd+Shift+X), screenshots, console logs, "Auto-verify" (`getAutoVerify/setAutoVerify`), Chrome login import, and
  remote-session preview over a framebuffer stream (`FramebufferPreview.*`). "Claude can start your dev servers… verify changes with
  screenshots, snapshots, and DOM inspection". VERIFIED.
- **Terminal:** integrated PTY (`node-pty`, `pty-host` worker), multiple terminal tabs (New/Rename/Close), shell chooser
  (`getTerminalShells`), toggle with Cmd+J (mac) or Ctrl+\`, "Attach terminal output" as context (Cmd+Shift+L), and "Find in terminal".
  Shell-integration scripts ship for bash, zsh, fish and pwsh (`terminal-shell-integration/`). IPC `startShellPty/writeShellPty/
  resizeShellPty/runBashCommand` (control `start_shell_task`). VERIFIED.
- **Mobile simulators:** iOS Simulator and Android Emulator panes (`Simulator.*`, 74 methods: streaming frames, gestures, recording,
  accessibility). VERIFIED.

## 9. Keyboard shortcuts & accessibility

The canonical renderer keybinding registry (`shared-1-*.js`, `{command, key, code, when, gate, mac}`) is VERIFIED. "cmd" means Ctrl
off macOS.

| Command | Keys |
|---|---|
| togglePreview | Cmd+Shift+B / Cmd+Shift+P (app); Cmd+Alt+P (web) |
| toggleDiff | Cmd+Shift+D (app); Ctrl+Shift+D (web) |
| toggleTerminal | Cmd+J (mac app); Ctrl+\` |
| toggleBrowser (Files) | Cmd+Shift+F |
| closePane / toggleExpandPane | Cmd+\\ / Cmd+Shift+\\ |
| cycleTranscriptMode | Ctrl+O |
| backgroundTasks | Ctrl+B |
| openModeMenu / openModelMenu / openEffortMenu | Cmd+Shift+M (or Cmd+Alt+M) / Cmd+Shift+I / Cmd+Shift+E |
| toggleFastMode | Cmd+Alt+F |
| toggleSelectionMode / toggleSketchMode | Cmd+Shift+S / Cmd+Shift+X |
| jumpPrev/NextPrompt | Cmd+Alt+↑/↓ (mac), Alt+↑/↓ |
| next/prevDiffChange | F7 / Shift+F7 (Alt+F5 non-Linux) |
| next/prevPrComment | Alt+F9 / Shift+Alt+F9 (non-Linux) |
| toggleSideChat | Cmd+; |
| newPreviewTab / reopenClosed | Cmd+T / Cmd+Shift+T |
| toggleDiffFileList | Cmd+Shift+Y |
| goToDiffFile / saveFile / findInFile | Cmd+P / Cmd+S / Cmd+F |
| attachTerminalOutput | Cmd+Shift+L |
| selectPreset1–9 / newPreset | Cmd+Alt+1–9 / Cmd+Alt+0 (Alt+n off mac) |
| selectBrowserTab1–9 | Cmd+1–9 |

The shortcuts dialog (Cmd+/) adds:
- **Sessions:** Cmd+K palette, Cmd+B sidebar, Cmd+W close tab/pane/session, Cmd+Shift+T reopen closed session, Cmd+1…9 jump to session,
  Cmd+Shift+Backspace archive.
- **Split view:** Alt+click opens a session in split view; Ctrl+] / Ctrl+[ cycle split panes.
- **Session actions:** Cmd+Alt+P pin, Cmd+Alt+R rename, Cmd+Alt+A archive, Cmd+Alt+U read/unread, Cmd+Alt+L copy link, Cmd+Alt+G open
  PR, Cmd+Alt+O fork.
- **Transcript and composer:** Esc stops; Esc Esc rewinds or edits the last message; Ctrl+R searches prompts; 1…9 selects a menu item;
  Cmd+U attaches a file.

Main-menu accelerators: Cmd+N New Session, Cmd+; New Side Chat, Cmd+\\ Close Pane, Cmd+Shift+O Open Folder, Ctrl+Alt+Shift+O Open
File, Cmd+, Settings, Cmd+/ Keyboard Shortcuts, F1 Help. IPC `MenuEvents.cycleSession`, `findInFiles`, `newWindow`, `reopenClosedSession`,
`toggleSessionPane`. VERIFIED.

Accessibility: "Motion" setting, "Transcript text size/width", "Interface font", "Chat font", screen-reader position labels ("{row},
position {position} of {count} in the sidebar"), and keyboard-navigable split panes. VERIFIED. Beyond that, no dedicated a11y settings
page was found.

## 10. Remote / mobile / multi-device

- **Remote Control:** serves a local session to claude.ai and the mobile app. IPC `toggleRemoteControl`, `enableCliRemoteControl`,
  `RemoteControlServing.*` (folders, pin, worktree). Per-session `remoteControlUserEnabled` / `bridgeSessionIds` /
  `steeredByRemoteClient`. `remote-control-state.json {version, identities, telemetrySalt}`. "Turn on Remote Control for all new local
  sessions". "You can then follow and steer it from claude.ai and the Claude mobile app." VERIFIED.
- **Dispatch:** phone → desktop task / code session handoff. "Dispatch a task or a code session from the mobile app"; push notifications
  "when Claude messages you in Dispatch"; "Prevents sleep while Dispatch is running". VERIFIED.
- **Cloud sessions / Claude Code on the web:** move to cloud (teleport), cloud environments (env vars, setup script), "Will run in the
  cloud only", and `CloudMove.*`. VERIFIED.
- **SSH remote hosts:** `ssh-remote-server-state.json {tokens, pendingDaemonUpgrades}`. A desktop-managed daemon is installed on the
  remote and upgraded on demand ("Updating now interrupts every session running on {host}"). VERIFIED.
- **Peer sessions:** "listening Claude sessions" auto-reply to Artifact threads; peer messages between sessions (`peerReceipts`,
  `markPeerMessagesRead`). VERIFIED.
- **Device registry:** `DeviceRegistry.*` (19), `ant-device-registry.json`, "Require trusted devices", "Remote sessions in Capabilities".
  VERIFIED (I did not read the file contents).
- **Multi-account:** session storage is partitioned by account and org UUID (5 account dirs on this host);
  `ccd-session-presets.json.byAccount{<acct>:{presets[], builtIns, seededFromDraft}}`; `lastKnownAccountUuid`. Only one account is
  active at a time (`Account.activeAccountChanged`). VERIFIED-from-userdata. I found no evidence of usage-aware automatic account rotation.

## Not found / gaps

- I found no hooks editor UI for Code (only labels in plugin/skill detail views).
- I could not isolate the AskUserQuestion multiple-choice widget strings (only the generic waiting/answer strings).
- I found no automatic multi-account rotation.
- I recorded the `Cache` / `Code Cache` dirs but did not mine them; the renderer ships locally in `ion-dist.asar`.
