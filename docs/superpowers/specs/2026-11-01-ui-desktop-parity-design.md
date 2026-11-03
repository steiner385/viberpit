# Sub-spec D — Desktop-parity session workspace

**Status:** approved design, 2026-11-01 · **Parent:** `2026-09-02-ui-target-state-design.md` (umbrella;
this sub-spec replaces its §5 table as the source of truth for Desktop alignment) · **Evidence and
decisions:** `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` (benchmark §3, owner decisions D1–D21
in §7; this spec cites them as `[A-Dn]`) · **Benchmark:** `scripts/desktop_parity_benchmark.py`.

The benchmark measured the web UI at **61% weighted parity** with the Claude Desktop *Code* mode (Linux build
4.4212.0) and 63% of the owner-approved target. This spec defines the target that closes the gap, the backend
seams it needs, and the order to ship it in.

---

## 1. Principles

1. **Desktop's vocabulary and placement win where both products have an opinion** (umbrella principle 2).
   ViberPit-only capabilities (accounts, fleet, autopilot, pipeline, kiosk) keep their own design.
2. **Phone is a first-class layout, not a degraded desktop.** Every surface below names its phone form.
   Desktop's tiling and windowing model is not adopted `[A-D1]`.
3. **One mechanism per concept.** Edit & resend is rewind plus a prefilled composer `[A-D7]`. Inline tool diffs
   reuse the Changes renderer `[A-D19]`. Side chat is the Assistant drawer `[A-D18]`. Slash commands and buttons
   call the same actions `[A-D15]`. "Allow for this session" is an always-allow rule with `scope=session` from the
   2026-10-11 rules design, not a new grant cache `[A-D11]`.
4. **Engine-neutral via capabilities.** Each new action is gated on an `AgentCapabilities` flag
   (`server/sessions/engine.py`, mirrored in `web/src/session/engineCapabilities.ts`). A Codex session hides
   what its engine cannot do; it never shows a dead button.
5. **Verify against the real binary first** (constitution III). Fork, rewind and checkpoints, fast mode, output
   styles and `--resume-session-at` are each proven against the installed CLI before UI work starts on them.

## 2. Session workspace layout

The rail switches on the session pane's own width (64rem, a container query), not the viewport; below that its cards move to a Details tab.

```
┌ session.header ─────────────────────────────────────────────────────────────┐
│ ← title (rename) · StatusPill · [+12 −1] chip · [PR #N ✓] chip · Views ▾ · ⋮ │
├ session.tabs: Conversation | Changes | Files | PR | Browser ───┬ session.rail ┤
│                                                                │ Changes      │
│  session.transcript  (or the active tab's pane)                │ Context      │
│                                                                │ Environment  │
│  session.pinned (permission / question / your-turn panels)     │ Tasks        │
├ app.dock: prompt row ──────────────────────────────────────────┴──────────────┤
│ [+] [ text … @ / ]                                         [Queue|Send] [Stop]│
│ mode ▾ · model ▾ · effort ▾ · ⚡fast · ◔ usage                                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Tabs** `[A-D1, A-D19]`: Conversation, **Changes**, **Files** (read-only), **PR**, Browser. A tab appears only when
  meaningful: Changes once the worktree differs from base, PR once a PR exists. The `Views ▾` menu lists the
  same entries, and the tab is part of the URL (`?view=changes&file=…&line=…`). This replaces the umbrella's
  `session.panes` (Browser / Tasks / PR).
- **Rail** `[A-D12]`: on desktop ≥1280px a collapsible right column; below that, and on phone, a **Details** tab.
  It has four cards:
  - **Changes**: `+N −M` and the file list, linking to the Changes tab.
  - **Context**: the context meter with an "Auto-compacts soon" warning and a `/compact` action `[A-D17]`, the
    loaded **memory files** (repo, user and imports, each viewable) `[A-D8]`, and active MCP servers.
  - **Environment**: host, account, worktree path and branch, with actions open PR / discard worktree /
    repair `[A-D16]`, plus sandbox state and the PR auto-fix toggle `[A-D16]`.
  - **Tasks**: background tasks and sub-agents.

  The rail **replaces `session.meta`** (the dense strip under the header). Its contents move into the rail or the
  prompt row, and the strip is deleted in the same PR.
- **Assistant drawer as side chat** `[A-D18]`: ⌘; and `/btw` open the drawer. Inside a session it is scoped to that
  session: it reads the transcript and diff through the assistant tools and never writes to the session.
  Outside a session it stays app-level.

## 3. Prompt row (`app.dock`, `SessionComposer`)

| Control | Behaviour | Ref |
|---|---|---|
| `+` menu | attach file/photo · slash commands · presets | `[A-D16]` |
| `@` | One menu with two groups. **Files** come from the session's worktree (fuzzy; inserted as `@path`, which the CLI resolves natively). **Sessions** are other sessions (a ViberPit reference). | `[A-D2]` |
| Mode ▾ (Alt+Shift+M) | Changeable mid-session through the driver's live permission-mode switch; see §3.1. The in-session Plan toggle is removed. | `[A-D10]` |
| Model ▾ (Alt+Shift+I) | Moves here from `session.meta`. The gated-model approval flow is unchanged. | `[A-D14]` |
| Effort ▾ (Alt+Shift+E) | low / medium / high / max, changeable mid-session; only where the model supports it. | `[A-D14]` |
| ⚡ Fast | Toggle; shown only where the model and engine support it. | `[A-D14]` |
| ◔ Usage ring | Wires the existing `ui/UsageRing.tsx` (context plus plan usage). Tap opens the Context card. | `[A-D14]` |
| Send / Queue / Send now / Stop | Unchanged. | — |
| **Esc** | While a turn runs, Esc **stops** it and shows a "Stopped · Esc" toast. When idle, Esc detaches as today. ← always detaches. Amends umbrella D8. | `[A-D9]` |

### 3.1 Permission modes `[A-D10]`

Desktop names come first, the ViberPit-only modes form a labelled group below, and **keys are unchanged** so
stored settings and automations do not migrate.

| Label | Key | Note |
|---|---|---|
| Ask | `interactive` | Desktop "Ask" (`default`) |
| Accept edits | `acceptEdits` | |
| Plan | `plan` | folds in today's Plan toggle |
| Auto | `auto` | **Semantic difference:** ViberPit's `auto` approves everything not on its danger denylist (`server/sessions/permissions.py:283`); Desktop's Auto is the CLI's classifier mode. The label is adopted now. Moving `auto` onto the classifier is a follow-up decision, gated on the classifier being available to every account type. |
| Bypass | `bypassPermissions` | |
| *ViberPit:* Read-only allowlist | `allowlist` | |
| *ViberPit:* Full auto | `full-auto` | |

`PERMISSION_MODES` (`web/src/session/permissionModes.ts`) stays the single list. It gains `group` and the new
labels.

## 4. Transcript

- **View modes** `[A-D13]`: Normal / Verbose / Thinking in the Views menu, cycled with Ctrl+O. The Settings value
  becomes the default.
- **Message menu** `[A-D3, A-D7]`: on hover, or a long press on phone.
  - Every message: **Copy as Markdown**.
  - User messages: **Fork from here**, **Rewind to here**, **Edit & resend**.
  - Assistant messages: Fork from here.
  - **Shipped (D-8, D-9).** Rewind to here and Edit & resend appear on a user turn once it has a CLI prompt uuid
    (live: the `userPromptUuid` echo; replay: the turn id). They are hidden without the `rewind` capability and
    disabled while a turn runs or a permission, plan or question card is pending. A rewind replaces the cut turns
    with a divider, `Rewound — N files restored`, carrying **Undo rewind**. The divider hides after the next
    prompt or once the undo is done. Replay follows the active chain, so cut turns stay hidden on reload. See §5
    for the mechanism and the undo design.
- **File paths** `[A-D13]`: path-like spans open the Files tab at that line. Secondary actions: Attach as `@`,
  Copy path.
- **Search** `[A-D13]`: ⌘F with focus in the transcript (falling through to the browser's find elsewhere). The
  search runs **server-side** over the full event log, so unloaded pages are found, with next/prev hits.
- **Post-turn summary card** `[A-D13]`: completed / ready for review / blocked, with files changed and a
  `+N −M` summary. It **subsumes** the "Likely complete" panel (`YourTurnPanel`); that branch is deleted in the
  same PR.
- **Inline diffs** `[A-D19]`: rendered by the Changes renderer (syntax-highlighted), with "Open in Changes".
  `transcript/DiffView.tsx` is replaced, not kept alongside.

## 5. Branching

| Action | Semantics | Backend |
|---|---|---|
| **Fork from here** `[A-D3]` | Creates a **new linked child session** (`parentSessionId`) whose conversation is the source up to that message, in its own worktree branched from the source's current HEAD. The source is untouched. | **Shipped (D-8), verified** (`2026-11-02-d8-fork-verification.md`): `resume=<source>` + `fork_session=True` + `extra_args={"resume-session-at": <uuid>}`. Claude only, behind `VIBERPIT_FORK_ENABLED` |
| **Rewind to here** `[A-D3]` | Truncates the conversation to before the message **and** restores files to that checkpoint. A divider marks the rewind and offers **Undo rewind**, which restores conversation and files. A confirmation lists the files that will change. | **Shipped (D-9), verified** (`2026-11-03-d9-rewind-verification.md`): `enable_file_checkpointing=True` on every spawn, then `rewind_files` (dry run and real run) and `rewind_conversation` control requests on the live client, inside a driver rewind hold. Undo is a ViberPit-owned backup plus byte truncation. Claude only, behind `VIBERPIT_REWIND_ENABLED` |
| **Edit & resend** `[A-D7]` | Rewind to before the user message, then prefill the composer with its text. | the rewind path; **shipped (D-9)**, prefilled from the CLI's `prefillText` |
| **Move to host** `[A-D19]` | Pushes the branch, then resumes the session on the target fleet host, keeping the session id and transcript. The source worktree is kept until the move is confirmed. | new orchestrator op over the existing fleet router |

Rewind and edit are disabled during a running turn and on engines without the capability.

**Fork as shipped (D-8).**
- **Invocation, verified against CLI 2.1.316 and SDK 0.2.105.** The child spawns with `resume=<source engine session>`,
  `fork_session=True` and `extra_args={"resume-session-at": <uuid>}`. The Python SDK has no `resume_session_at` option,
  so `extra_args` builds the same argv the TS SDK does. `--resume-session-at` is a **hidden** flag (`.hideHelp()`), and
  stream-json mode honours it. The cut is **inclusive**: the anchor is the last top-level assistant entry of the
  reply, so the fork keeps that reply and continues after it. The source transcript is untouched. The gate is
  `engine.fork_supported`, which reads the Claude capability and the kill switch `VIBERPIT_FORK_ENABLED` (default on).
  Codex stays non-forking.
- **Forking from your own message** works as edit-and-branch. The fork cuts at the **previous reply's** anchor, and
  the child's composer is prefilled with the message text, so you can edit it before sending. Forking the **first**
  message, which has no previous reply, starts a fresh session with the same prefill.
- **Files** start from the source's **current** worktree, not from the fork point. The child gets a new branch
  `<prefix>fork-<id>-<n>` at the source's HEAD, with uncommitted and untracked files copied. File restore to a
  message is rewind (D-9).

**Rewind as shipped (D-9).**
- **Checkpointing.** Every coding spawn (new, resume, fork and the driver's fresh-session respawn) passes
  `enable_file_checkpointing=True` while the kill switch `VIBERPIT_REWIND_ENABLED` (default on) is on. The CLI then
  snapshots, per user prompt, the files that **Edit, Write and NotebookEdit** touch. Bash is never tracked. It keeps
  the newest **100** snapshots per session. `file-history` joins `SHARE_ALLOWLIST` in `src/viberpit/configdir.py`,
  so every account config dir links one shared store and a rotated session can still restore its files. Without
  that, a rewind after rotation was a silent partial restore (verification §4). Credentials are still never shared.
- **Target.** The client sends `toTurnId`, the user turn's id, as fork sends `atTurnId`. The server resolves it to
  the CLI prompt uuid. Live, `extra_args["replay-user-messages"]` makes the CLI echo each prompt with its uuid, and
  the translator emits `userPromptUuid {turnId, uuid}`. On replay the turn id already is the uuid. A non-user turn
  is refused `REWIND_NOT_USER_MESSAGE`.
- **Verified mechanism (CLI 2.1.317, SDK 0.2.105).** One `driver.rewind_hold()` wraps the whole sequence on the
  idle live client. Prompts sent meanwhile queue in the driver and start, in order, when the hold is released.
  1. `rewind_files {user_message_id, dry_run:true}`. This is the confirmation source and gives `filesChanged`,
     `insertions` and `deletions`. "No file checkpoint found" or "not enabled" gives `checkpointAvailable:false`,
     and the UI offers a conversation-only rewind.
  2. The undo backup (below).
  3. `rewind_files {user_message_id}` (the real run), unless the mode is `conversation`. Files go first, because the
     cut removes the target's snapshot from the chain.
  4. Record the transcript's byte length.
  5. `rewind_conversation {target_message_uuid, last_seen_user_message_uuid}`. The cut is **exclusive**: it drops
     the target prompt and everything after it, appends a `last-prompt` row with `rewound:true`, and returns
     `prefillText`, the prompt's text. The live client carries on with no respawn.
  6. Emit `rewound {rewindId, toTurnId, promptUuid, filesRestored}`.

  Both control requests are an internal CLI surface (constitution I). They go through the SDK's private
  `_send_control_request`, because the Python SDK's `rewind_files()` drops the reply and has no dry run. A no-model
  contract test pins them against the installed binary. Every CLI `reason` maps to a code in `REWIND_CODES`
  (`server/sessions/rewind.py`). If a step after the backup fails, the backup is restored. If the cut's outcome is
  unknown, the client is stopped, the transcript truncated to the recorded length, the files restored and the
  session resumed.
- **Undo design: a ViberPit-owned pre-rewind checkpoint.** The CLI cannot undo: it never snapshots the state after
  the last turn, the cut makes the dropped turns' snapshots unreachable, and it has no redo.
  - **Backup.** Taken after the confirm and before the real `rewind_files`. It covers the dry-run `filesChanged`
    plus every path the transcript's `file-history` rows track from the target onward, because a mode-only revert
    is restored but not previewed. Each path's bytes and mode are stored, or "absent". It lives under
    `VIBERPIT_HOME/rewind/<sessionId>/<rewindId>/` on the session's host, and is written through
    `src/viberpit/fs_beneath.py` without following symlinks. Over **64 MiB** in total, the rewind is refused with
    `REWIND_TOO_LARGE` and nothing changes.
  - **Undo** stops the client, restores the files, truncates the transcript back to the recorded byte length, and
    resumes the same engine session with `--resume`. It then emits `rewindUndone`. The second probe showed the
    model and the CLI's checkpoints see the pre-rewind conversation again.
  - **The tail allowlist.** Truncation is allowed only when every row past the recorded length is one of the rows
    a rewind or an idle exit appends: `last-prompt`, `cost-state` or `tools-used` with no `uuid` (third probe).
    Anything else, including a torn line, means a turn happened, so undo refuses `REWIND_UNDO_EXPIRED`. The
    check runs again after the client stops.
  - **The latest rewind only.** Each new rewind expires the older ones. The next prompt sent to the session expires
    the rewind and deletes its backup. Archive deletes all of the session's backups. Undo is idempotent by
    `rewindId`. A partial restore keeps the backup and refuses `REWIND_FAILED` with the failed paths, so a retry
    can finish the job.
  - **Persistence.** One `SessionRewind` row per rewind (migration `0095_session_rewinds`) holds the target, the
    transcript path and length, the backup dir, the host, the files restored, the prefill and the
    `created_at` / `undone_at` / `expired_at` stamps.
- **Replay follows the active chain.** The dropped rows stay in the JSONL, so `replay.py` drops off-chain prompts,
  and everything under them, once an explicit `rewound` `last-prompt` row is present. This governs D-8's fork
  anchors too, so cut turns are never fork points.
- **Rewind & retry** (§6) rewinds to the failed turn's prompt, then sends `prefill` unchanged through the normal
  send path. It is offered on the latest prompt's own failure only, and not for image, nudge or text-less prompts.
  Because the resend is a prompt, that rewind cannot be undone.
- **`/rewind`** is an app command in `catalog.py`, gated like the route. It opens a picker of the 20 newest prompts.

## 6. Feedback

- **Permission card** `[A-D11]`: **Allow once · Allow for this session · Always allow… · Deny** (⌘. denies).
  "Allow for this session" writes an always-allow rule with `scope=session`, using the 2026-10-11 rules store.
  It is listed in, and revocable from, the session rules readout.
- **Plan approval** `[A-D17]`: Approve & run · Keep planning **with comments**. Comments can be overall or per
  plan step, and are sent back as one structured message.
- **Error cards** `[A-D17]`: in addition to auto-resend and rotation, **Switch model**, **Lower effort** and
  **Rewind & retry** (the latter where the capability exists). **Rewind & retry shipped (D-9)**; see §5.

## 7. Sidebar and navigation

- **Group/sort** `[A-D12]`: group by none / repo / status / **branch** / **host** / **date** / **PR state**; sort
  is unchanged. `session/ordering.ts` stays the single owner.
- **Read/unread** `[A-D12]`: a session is unread when it has assistant output newer than the viewer's per-user
  `last_viewed_seq`, which is stored server-side so it follows the user across devices. Unread shows as a bold
  title, and the card menu has Mark read/unread. It complements the amber needs-you badge and does not replace it.
- **⌘K palette** `[A-D5]`: ⌘K/Ctrl+K opens a palette over sessions (including archived), repos and branches,
  sections (`navDefs`), settings sections (`settingsDefs`) and app actions (`shortcuts`). It mounts in
  `app.overlays` at `z-palette`. It adds no new index of its own; every entry derives from an existing registry.
- **Keyboard** `[A-D20]`: all bindings are registered in `shell/shortcuts.ts`, and each is checked against
  browser-reserved chords (Chrome on Linux/macOS/Windows) in a test.

| Chord | Action |
|---|---|
| Alt+Shift+M / I / E (⌥⇧ on Apple) | mode / model / effort menus. Desktop's ⌘⇧M/I/E is browser-reserved (Ctrl+Shift+I DevTools, ⌘/Ctrl+Shift+M profile menu), so this is the fallback chord; matched on `e.code`. |
| ⌘⌥↑ / ⌘⌥↓ (Alt+↑/↓ on Linux/Windows) | jump to previous / next own prompt. On Chrome Mac ⌘⌥↑/↓ shadows pane focus, which is not reserved, so the page may take it. It is chosen over ⌥↑/↓ because that one moves the caret in a textarea. Never ⌘⌥←/→, which is Chrome Mac's previous/next tab. |
| ⌘⌥[ / ⌘⌥] (Alt+[ / Alt+] on Linux/Windows) | previous / next session in list order. A bare ⌥ chord types on Mac non-US layouts (German ⌥5/⌥6 are `[` `]`), so Apple adds ⌘. |
| ⌘⌥1–9 (Alt+Shift+1–9 on Linux/Windows) | jump to the nth session in list order; presets in the dispatch bar. Not bare Alt+1–9: Chrome and Firefox on Linux use it to select tabs, and taking it would break tab switching. |
| ⌘K | palette |
| Ctrl+O | cycle transcript view mode |
| Esc | stop while busy (§3) |

## 8. Configuration surfaces

| Surface | Target | Ref |
|---|---|---|
| Memory files | The Context card lists every loaded file. **Settings › Claude config › Memory** edits the **user-level** `CLAUDE.md` and syncs it across account config dirs and hosts. Repo `CLAUDE.md` is read-only in the UI; changes go through the agent and a PR. | `[A-D8]` |
| Plugins | Settings › Plugins gains marketplaces (add/remove), browse, install and update. | `[A-D15]` |
| MCP | Per-tool enable/disable per server (Settings) and per session (Context card). | `[A-D15]` |
| Output styles | Settings list and default; per-session picker in the `+` menu. | `[A-D15]` |
| App slash commands | `/fork /rewind /btw /plan /effort /fast /model` are added to `server/sessions/commands/catalog.py` and dispatch to the same actions as the UI. | `[A-D15]` |
| Presets | The dispatch bar's `+` menu and ⌘⌥1–9 select existing presets and templates, which today have no consumer. | `[A-D16]` |
| Worktrees | Settings › Sessions gains worktree location and branch prefix (today server-only). | `[A-D16]` |
| Sandbox | A session default plus a per-session toggle (Environment card). | `[A-D16]` |
| PR auto-fix | A per-session toggle mapped onto the existing PR Doctor / autopilot, with no new mechanism. | `[A-D16]` |
| Dev-server preview | Detect listening dev servers started by the session and offer them in the Browser tab (VNC) or a proxied preview with element select. | `[A-D19]` |

## 9. Rejected for this target (with reasons)

| Desktop feature | Reason | Ref |
|---|---|---|
| Split view / tiled panes, pop-out windows | Desktop-only windowing model that fights the phone layout; tabs plus the rail cover the review need. | `[A-D1, A-D21]` |
| Integrated terminal, file editing | The agent runs the shell; a browser PTY and editor are large, security-sensitive surfaces with low marginal value. | `[A-D1]` |
| Pin / star sessions | Read/unread plus needs-you plus grouping cover triage. | `[A-D12]` |
| Ctrl+R prompt search | ↑/↓ history is enough. | `[A-D20]` |
| Per-event notification levels, font/transcript size, routines in the session UI | Existing channels, routing rules, kiosk scale and Delivery schedules cover them. | `[A-D21]` |

## 10. Backend seams

| Seam | Needed by |
|---|---|
| `GET /sessions/{id}/files?q=` (worktree file list, per host via the fleet router) — **shipped (D-7)** | `@` files, Files tab, palette |
| `GET /sessions/{id}/files/{path}` (read-only content, with a size cap) — **shipped (D-7)** as `GET /sessions/{id}/files/content?path=` | Files tab, memory-file viewer |
| `GET /sessions/{id}/diff` (aggregate worktree diff vs base, structured hunks) | Changes tab, chip, rail, inline diffs |
| `POST /sessions/{id}/review-comments` (batch → one prompt) | not needed — client-composed prompt (D-5) |
| `POST /sessions/{id}/fork` `{at_message_id}` — **shipped (D-8)** as `{atTurnId, mode, prefill?, requestId}` | Fork |
| `POST /sessions/{id}/rewind` `{to_message_id}` + `POST …/rewind/undo` — **shipped (D-9)** as `POST /sessions/{id}/rewind` `{toTurnId, mode: "files_and_conversation" \| "conversation", dryRun?, requestId}` → dry run `{files, deleted, insertions, deletions, checkpointAvailable}`, real run `{rewindId, prefill, filesRestored}`; and `POST …/rewind/undo` `{rewindId}` → `{ok, skipped}`. 404 for an unknown session, 409 `{code, message}` with a `REWIND_CODES` code, 409 `daemon_outdated` | Rewind, Edit & resend, Rewind & retry |
| live `set_permission_mode` / effort / fast on the driver | prompt row |
| `scope=session` rule creation from the card | permission card |
| per-viewer `last_viewed_seq` | read/unread |
| `GET /sessions/{id}/search?q=` | transcript search |
| memory-file discovery (loaded `CLAUDE.md` set) + user-level write/sync | Context card, Settings › Memory |
| MCP per-tool toggles, output styles, plugin marketplace ops | Settings |
| `POST /sessions/{id}/move` `{host}` | Move to host |
| dev-server detection on the session's host | Preview |

Every seam that reaches the session daemon also needs `RemoteOrchestrator` / `FleetOrchestrator` routing.
Private orchestrator seams are inert on the remote orchestrator; add real RPCs.

## 11. Sequence

Each PR re-runs `scripts/desktop_parity_benchmark.py`, flips the rows it closes, and pastes the new §3 into the
audit. Ordered by points closed per unit of risk, with dependencies first.

| # | PR | Closes (benchmark rows) |
|---|---|---|
| D-1 | Prompt row: mode picker (relabel, Plan folded in), model, effort, usage ring; Esc-stops; ⌘⇧M/I/E | mode picker, model, effort, usage ring, Esc, keyboard menus |
| D-2 | Permission card four buttons (`scope=session`); plan comments | permission card, plan approval |
| D-3 | Message menu: Copy as Markdown; view modes and Ctrl+O | copy, view modes |
| D-4 | Diff endpoint + Changes tab + chip + rail Changes card + shared renderer for inline diffs | Changes pane, inline diffs |
| D-5 | Line comments → one prompt; per-step plan comments land with the same line-comment component | (completes Changes pane) |
| D-6 | Session rail (Context / Environment / Tasks) replacing `session.meta`; worktree and branch display and actions; context warnings | rail, worktree, context meter |
| D-7 | File-list/read endpoints; `@` files + sessions; Files tab; clickable paths | `@` mentions, file paths, files pane |
| D-8 | Verify fork against the binary → Fork from here + `/fork` | fork |
| D-9 | Verify checkpoints → Rewind (+ undo) + Edit & resend + Rewind & retry + `/rewind` | rewind, edit & resend, recovery actions |
| D-10 | ⌘K palette; group/sort keys; read/unread; session-switch and jump-to-prompt keys | palette, grouping, unread, keyboard |
| D-11 | Transcript search; post-turn summary card (deletes the Likely-complete branch) | search, summary card |
| D-12 | Context-aware Assistant (⌘;, `/btw`) | side chat |
| D-13 | Memory files (Context card + Settings › Memory) | CLAUDE.md |
| D-14 | Presets in dispatch; worktree/branch-prefix settings; sandbox; PR auto-fix toggle; fast mode | presets, worktree settings, sandbox, PR behaviour, fast |
| D-15 | PR tab | PR viewer |
| D-16 | Plugins marketplace; MCP per-tool toggles; output styles; remaining app slash commands | plugins, MCP, output styles, slash commands |
| D-17 | Move session to host | move session |
| D-18 | Dev-server detection + preview | preview |

## 12. Success criteria

- `scripts/desktop_parity_benchmark.py` reports **Target completion ≥ 95%**. Every non-rejected row is ✅, or ◐
  with a recorded reason.
- Every new action is hidden, not disabled-and-dead, on an engine lacking the capability (vitest per action).
- Each surface in §2–§8 renders at 390, 820 and 1440px in the Playwright viewport projects, and the e2e spec
  for each changed surface is updated in the same PR.
- Replaced code is deleted in the PR that replaces it: `session.meta` strip, in-session Plan toggle,
  `transcript/DiffView.tsx`, the "Likely complete" branch.
