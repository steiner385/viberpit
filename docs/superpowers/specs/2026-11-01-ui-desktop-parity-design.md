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
  **PR tab as built (D-15).** Listed when the session has any PR (`Session.prs`), after Changes, and lazy-loaded.
  Its data is `Session.prs` plus `GET /api/prs/{o}/{n}/{num}`, polled every 30 s while the tab is visible; when the
  live read fails the route falls back to the session's per-branch cache and the tab shows "Live details
  unavailable". The tab has a picker (lowest-numbered open or draft PR by default; shown only with several PRs), a
  header with state, auto-merge, merge-queue (read-only) and hold badges, checks grouped failing / pending / passing,
  and review threads (unresolved first, resolved collapsed, outdated chipped). A thread's `path:line` links to the
  Files tab only when the file exists in the worktree. **Ask session to address** prefills the composer with the
  thread and switches to Conversation; it never sends, and asks before replacing unsent text. Actions are Arm
  auto-merge (gated by the existing `readyMergeBlock`), Disable, Merge now (squash, behind a confirm; only when the
  merge state is `CLEAN` and the PR is not a draft) and Fix. The merge-state line uses fixed copy per GitHub
  `mergeStateStatus` (`DRAFT` and unknown values show as-is). **`hold:human` guard:** merge-now, arm and the Pipeline
  page's ready-merge all answer 409 `PR_HUMAN_HOLD` for a held PR. The label is read live from GitHub, never from the
  cache; if that read fails they fail closed with 409 `PR_HUMAN_HOLD_UNKNOWN`. Disable auto-merge is always allowed.
  Mutations write `pr.merge`, `pr.auto_merge_disabled` and `pr.ready_merge` audit rows, and neither new route is on
  the Assistant's allowlist. **Untrusted content:** comment and thread bodies render only through the sanitized
  Markdown renderer, which fetches no remote images, and a link renders only when it is an absolute http(s) URL.
- **Rail** `[A-D12]`: on desktop ≥1280px a collapsible right column; below that, and on phone, a **Details** tab.
  It has four cards:
  - **Changes**: `+N −M` and the file list, linking to the Changes tab.
  - **Context**: the context meter with an "Auto-compacts soon" warning and a `/compact` action `[A-D17]`, the
    loaded **memory files** (repo, user and imports, each viewable) `[A-D8]`, and active MCP servers.
    As built (D-13), the memory list is the CLI's own loaded set, `get_context_usage().memoryFiles`
    (`[{path, type, tokens}]`, load order, absolute paths, the CLI's types). It rides the `contextUsage` event, is
    persisted on the session (`sessions.memory_files`) and is re-emitted on attach. Each row shows a label (the
    CLI type; `AutoMem` reads "Auto memory"), the path worktree-relative inside the worktree and absolute
    otherwise (cut from the start, so the file name stays), and its tokens. The list is hidden when the set is
    empty, and Codex reports none. A row opens a read-only viewer (the Files tab's frame), and the user file adds
    **Edit in Settings**.
  - **Environment**: host, account, worktree path and branch, with actions open PR / discard worktree /
    repair `[A-D16]`, plus sandbox state and the PR auto-fix toggle `[A-D16]`.
    Of those three actions, D-19 shipped two, **Discard and Repair**; "open PR" is the D-15 **PR tab** (the
    card itself only lists the session's PRs). Discard and Repair work for managed worktrees, in a human session only (hidden on a 403 or a
    failed preflight); `GET /sessions/{id}/worktree/preflight`, `POST .../worktree/discard {confirm: true}`,
    `POST .../worktree/repair`, per session through the RPC, remote, fleet and facade layers.
    - **Discard** means end the session and remove its worktree. It runs for an **idle** session only (a running
      turn refuses), for a **person** (never automation), after an explicit **confirm** in the dialog. The audit
      row (`session.worktree_discard`, success or failure) is written **on the daemon**, so it survives an edge
      that never sees the result. Work is never destroyed unrecoverably: a dirty tree or unpushed commits always
      get a **recovery ref** first, and when git cannot say whether commits would be lost the discard **fails
      closed** (409 `WORKTREE_DISCARD_UNSAFE`, nothing touched). Only a **session-prefixed** branch
      (`brand.is_session_branch`) is deleted; a user branch is kept. **Gitignored files (`.env`, build output)
      are not preserved**, and the dialog says so. The session is **archived**. The success toast lists every ref
      and the restore command.
    - **Repair** recreates a **missing or empty** tree only, through the existing `_ensure_session_cwd`; it never
      touches a present tree.
    - **Race guard:** the removal and recreate paths (discard, repair, `_reassign`, D-17's `_close_for_transfer`)
      re-check `_record_still_live` first, so a queue drain or attach that races a discard cannot recreate the
      tree it just removed. `remove_worktree` is fail-closed for the janitor, close and transfer too.
    - **Follow-ups:** showing a discarded session's recovery refs on the archived session (needs server and
      transcript work); pin / star sessions, which has no decision yet.
  - **Tasks**: background tasks and sub-agents.

  The rail **replaces `session.meta`** (the dense strip under the header). Its contents move into the rail or the
  prompt row, and the strip is deleted in the same PR.
- **Assistant drawer as side chat** `[A-D18]`: ⌘; and `/btw` open the drawer. Inside a session it is scoped to that
  session: it reads the transcript and diff through the assistant tools and never writes to the session.
  Outside a session it stays app-level. As built (D-12):
  - **Scope comes from the server.** It is the `sessionId` of the latest context the session's driver actually
    sent (`context_scope`, on both the Claude and Codex drivers, in the `AgentDriver` contract), read at tool-call
    time through the `current_scope` callable the overlay build hands to `build_tools`. Tool input never sets
    it. A rebuilt driver (rotation, recompose, crash recovery, idle reap) is seeded at build time with its
    predecessor's scope until it sends a context of its own. A new daemon process falls back to the journal: the
    newest context header on the active chain of the assistant's Claude transcript, so a D-9 rewind is honoured
    as replay honours it (`replay.last_prompt_context`), with an in-memory
    record taking precedence. The deploy-restore replay and `resume_interrupted` never auto-send an assistant
    turn. The Codex driver carries the same context header in its turn input, and Codex replay strips it back
    into `context`.
  - **Enforcement is one predicate in three places.** `scope_refusal` (`server/assistant/tools.py`) runs before
    the permission card in the Claude driver's `_can_use_tool` and in the Codex driver's dynamic-tool gate, so
    no card is raised, and again in `build_tools.guarded` before the handler, so no endpoint is called. The
    error is `SCOPED_READ_ONLY: This chat is about session X and can't change it — ask in the session itself,
    or close the side chat`, logged at INFO and audited and metered as `refused`.
  - **Targets are canonical.** `session_id_arg` admits only a bare session UUID (the route allowlist's own
    pattern) and lowercases it, and the handler is sent the id that was checked. A malformed target refuses
    the whole call while scoped. `AssistantApi.check_path` refuses any request path with a dot segment, `?`,
    `#`, `%`, a backslash or whitespace, so no other path argument can be normalised onto a session route.
  - **Every write tool is in one of three tables.** `SESSION_TARGET_KEYS` holds the tools that name sessions and
    are refused when they name the scoped one. `BLOCKED_WHILE_SCOPED` holds `trigger_automation_action` and
    `consolidate_prs`, which reach sessions their input does not name, so they are refused while any session is
    scoped. `NO_SESSION_TARGET` holds the rest, each with a reason; these are PR-, issue- and product-keyed
    writes and new-session starters, and they stay outside the guard. A test fails until a new write tool is
    classified.
  - **Read tools.** `get_session_diff` gives a per-file summary by default. With `summary=false` it reads
    `GET /api/sessions/{id}/diff?format=patch`, which renders hunks as unified text under one byte budget
    (`max_files` ≤ 300, `max_patch_bytes` ≤ 60 kB; the tool asks for 10 / 24 kB), reports every cut under
    `cut`, and redacts credential-shaped strings with `redact_text`. That redactor has known gaps (#3242).
    `search_session_transcript` searches inside one session (D-11's endpoint, ≤ 50 hits).
  - **The header shows the server's scope.** "About: <session>" comes from the newest started user turn that
    carries a context (`selectPromptScope`), not from client intent. While a turn runs or prompts are queued
    and the open session differs, it adds "Next message will be about <open session>".
  - **`/btw`** is described in §8.

## 3. Prompt row (`app.dock`, `SessionComposer`)

| Control | Behaviour | Ref |
|---|---|---|
| `+` menu | attach file/photo · slash commands · presets | `[A-D16]` |
| | **As built (D-14):** one `PlusMenu` on `ui/Menu`; a `›` entry swaps in a second menu on the same anchor. The **dispatch bar**'s holds Attach image (the old 📎 button is gone), Presets › and Templates ›. The **session composer**'s holds Attach file and Slash commands only, because presets apply at creation. Slash commands seeds `/` only into an empty box and never overwrites a draft. | |
| `@` | One menu with two groups. **Files** come from the session's worktree (fuzzy; inserted as `@path`, which the CLI resolves natively). **Sessions** are other sessions (a ViberPit reference). | `[A-D2]` |
| Mode ▾ (Alt+Shift+M) | Changeable mid-session through the driver's live permission-mode switch; see §3.1. The in-session Plan toggle is removed. | `[A-D10]` |
| Model ▾ (Alt+Shift+I) | Moves here from `session.meta`. The gated-model approval flow is unchanged. | `[A-D14]` |
| Effort ▾ (Alt+Shift+E) | low / medium / high / max, changeable mid-session; only where the model supports it. **As built (D-14):** Claude effort is now live. The existing `POST /sessions/{id}/model {effort}` applies `apply_flag_settings({effortLevel})` to the running CLI; there is no second effort route. The picker shows for Claude when the engine reports `flagSettings` and offers the CLI's levels (`low/medium/high/xhigh/max`). The server checks a pick against the model's `supportedEffortLevels` and refuses one the model lacks. Per-model levels in the UI shipped in #3269: the picker offers the current model's `supportedEffortLevels` (Claude) or the catalog's levels (Codex). | `[A-D14]` |
| ⚡ Fast | Toggle; shown only where the model and engine support it. **As built (D-14):** shown only when the payload's `fastModeAvailable` is true. That means the CLI's `initialize` model list marks the current model `supportsFastMode`; Copilot accounts never qualify. The tooltip, and a one-time inline note the first time fast mode is on in a browser, read: "Fast mode — faster output on the same model. Bills usage credits at a higher rate, outside your plan." (owner decision: a toggle with a cost note, off by default). `POST /sessions/{id}/fast {enabled}`; ⚡ is disabled while the POST is in flight. **Money rules:** the session record (`sessions.fast_mode`) is authoritative. Every model switch re-syncs the CLI to the record, and a switch to a model without fast mode (or with an unknown model list) clears it. A failed or unanswered enable is compensated and kept off; a failed disable never reads as off. ⚡ shows the stored choice; the CLI's live `fast_mode_state` / `fast_mode_disabled_reason` (read from `initialize` and each `system/init`, since the SDK's typed `ResultMessage` drops the copy on `result` frames) ride the payload as `fastModeState` / `fastModeDisabledReason`, and the tooltip leads with a cooldown or the disabled reason (#3254). | `[A-D14]` |
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
  - **Shipped (D-11).** The `transcript.search` row (⌘F / Ctrl+F) matches on `e.key`, with the physical `KeyF`
    only for non-Latin layouts, and Shift is excluded. It fires only with a session's transcript on screen (not on
    the Files / Changes / Browser / Details tabs) and focus in the transcript pane, the session composer, or
    nowhere. Anywhere else it is neither matched nor prevented, so the browser's find works. The focus test is a
    per-key context fact, so the ⌘K palette offers "Search this conversation" whenever a transcript is open. The
    transcript scroller takes focus on a click (`tabIndex={-1}`, no ring).
  - The bar is lazy (its own chunk, with the highlighters and the jump) and sits at the top of the transcript
    pane. It shows the query, `N of M` (`M+` when older matches were cut), Older / Newer buttons and ✕. Queries
    are debounced 250 ms, need at least 2 characters, and stale responses are dropped. A failed search toasts
    and reads "Search failed".
  - **Newest first.** Results land on the newest hit, but only when it is already loaded: typing never pages.
    Enter and ↑ go older, Shift+Enter and ↓ go newer, both wrapping; the count stays in transcript order. Esc
    closes the bar and returns focus to where ⌘F was pressed.
  - **Jumping.** A hit older than the loaded history pages older windows in contiguously through the
    transcript's own load-older path, at most 2000 turns back. The hit's row is then centred and held; in a turn
    taller than the viewport, the match itself is centred. The user taking over (a scroll, Scroll to bottom, a
    send, a prompt jump, a new query) abandons a jump that is still paging. Dead ends toast.
  - **Highlights.** Every term is marked in user prompts and assistant prose (never code, tools or thinking),
    and the current hit is drawn stronger.
- **Post-turn summary card** `[A-D13]`: completed / ready for review / blocked, with files changed and a
  `+N −M` summary. It **subsumes** the "Likely complete" panel.
  - **Shipped (D-11).** The card is the transcript's tail row: after the last turn, before the rewind divider.
    It shows once the session is not busy and its last reply has ended. The outcome uses `statusTier`'s labels:
    `complete` / `ended` → **Complete**, `handed_back` → **Handed back** + the reason (and the "Likely complete"
    line with **Mark complete** / **Not done**), `done` → **Your turn**, `needs_attention` → **Turn failed**,
    `limited` → its label. `waiting_*` and `ready_review` show no card, because their own cards own the moment.
    While busy with a hand-back reason it is the "Asked you" note. It lists up to 5 changed paths (`+K more`)
    with `+N −M` when every path is counted (Codex and notebook paths are not), plus **Open Changes**. The
    persistent live region announces the hand-off reason once.
  - The pinned Your-turn panel was deleted in the same change. The list card's "Likely complete" pill stays.
- **Inline diffs** `[A-D19]`: rendered by the Changes renderer (syntax-highlighted), with "Open in Changes".
  `transcript/DiffView.tsx` is replaced, not kept alongside.

## 5. Branching

| Action | Semantics | Backend |
|---|---|---|
| **Fork from here** `[A-D3]` | Creates a **new linked child session** (`parentSessionId`) whose conversation is the source up to that message, in its own worktree branched from the source's current HEAD. The source is untouched. | **Shipped (D-8), verified** (`2026-11-02-d8-fork-verification.md`): `resume=<source>` + `fork_session=True` + `extra_args={"resume-session-at": <uuid>}`. Claude only, behind `VIBERPIT_FORK_ENABLED` |
| **Rewind to here** `[A-D3]` | Truncates the conversation to before the message **and** restores files to that checkpoint. A divider marks the rewind and offers **Undo rewind**, which restores conversation and files. A confirmation lists the files that will change. | **Shipped (D-9), verified** (`2026-11-03-d9-rewind-verification.md`): `enable_file_checkpointing=True` on every spawn, then `rewind_files` (dry run and real run) and `rewind_conversation` control requests on the live client, inside a driver rewind hold. Undo is a ViberPit-owned backup plus byte truncation. Claude only, behind `VIBERPIT_REWIND_ENABLED` |
| **Edit & resend** `[A-D7]` | Rewind to before the user message, then prefill the composer with its text. | the rewind path; **shipped (D-9)**, prefilled from the CLI's `prefillText` |
| **Move to host** `[A-D19]` | Resumes an idle Claude session on another fleet host, keeping the session id, the transcript and the worktree path. The source worktree is removed only after the target's driver has started and a recovery snapshot exists. | **Shipped (D-17)**; see §8 for the design as built |

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
  **Switch model and Lower effort shipped (D-19).** Every `error` event carries a `kind`
  (`failure.ERROR_KINDS`, derived from `code` plus the raw text; records written earlier compute it on decode;
  a missing `kind` reads as `other`). The card offers actions by kind:
  - `rate_limit` and `overloaded`: **Switch model** and **Lower effort**, plus **Rewind & retry**.
  - `context_too_long`: **Compact** (when the command catalog lists `compact`, the check the Context card makes),
    plus **Rewind & retry**.
  - `model_unavailable`: **Switch model** only; Rewind & retry is hidden (a resend fails the same way).
  - `other` (including auth, `max_turns`, `execution_error`): Rewind & retry only.
  Recovery actions show in a live session only; auto-resend and rotation are unchanged.
  **Switch model** opens the model picker and never picks one. **Lower effort** steps one level down from the
  level in force, using the one reading the prompt row's picker uses (`effortChoices`); when levels exist but
  the level in force is unknown (a default Claude session) it opens the effort picker ("Lower effort…") and
  never guesses, and it is hidden at the floor or with no levels.

## 7. Sidebar and navigation

- **Group/sort** `[A-D12]`: group by none / repo / status / **branch** / **host** / **date** / **PR state**; sort
  is unchanged. `session/ordering.ts` stays the single owner. **Shipped (D-10):** `date` buckets `updatedAt`
  into Today / Yesterday / Previous 7 days / Older in local time; `pr` files a session under its most advanced
  PR (Open › Draft › Merged › Closed › No PR); sessions with no branch or host trail as "No branch" / "No host".
  Archived ordering is unchanged.
- **Read/unread** `[A-D12]`: unread shows as a bold title, and the card menu has Mark as read / Mark as unread.
  It complements the amber needs-you badge and does not replace it. **Shipped (D-10), with a deliberate
  deviation:** there is no `last_viewed_seq`. The daemon's event seq resets on restart, so nothing durable could
  be compared against it. Instead a per-viewer `session_views` row stores `viewed_at` (server-side, so it follows
  the viewer across devices), compared against the session's persisted `last_turn_at`:
  - a session is unread for a viewer iff that viewer has a row **and** `last_turn_at > viewed_at`;
  - **no row means read**, so pre-existing and automation-only sessions never turn bold on day one; opening a
    session creates the row;
  - "viewed" stores `max(DB now, last_turn_at)`, so a worker whose clock runs ahead of the DB cannot leave a
    session the viewer has just read bold; "Mark as unread" stores a timestamp just before `last_turn_at` (or
    drops the row when there is no turn yet);
  - the open session posts "viewed" on open, when the tab becomes visible again, and 2 s after each new turn end
    while it stays open; nothing posts from a hidden tab. Its own card never shows bold. Opening an archived
    session's Reader page marks it read too, and archived cards show the same bold.
- **⌘K palette** `[A-D5]`: ⌘K/Ctrl+K opens a palette over sessions (including archived), repos and branches,
  sections (`navDefs`), settings sections (`settingsDefs`) and app actions (`shortcuts`). It mounts in
  `app.overlays` at `z-palette`. It adds no new index of its own; every entry derives from an existing registry.
  **Shipped (D-10)** as `AppPalette` (`features/palette/`, lazy-loaded on first open):
  - sections in order: **Sessions** (live, background, then archived), **Actions** (New session plus every
    `app`/`in-session` shortcut row with a no-event handler whose `when` passes, showing its chord), **Navigate**
    (`NAV_DEFS`, kiosk-filtered), **Settings** (`SETTINGS_META`), **Repos** (`useGithubRepos`) and **Branches**
    (distinct live-session branches; archived sessions carry no branch). An empty query shows the 8 most recent
    sessions, then Actions. ↑/↓ move, Enter runs, Esc or ⌘K again closes.
  - **Repos and branches drill down inside the palette.** A repo the list's Project filter offers sets that
    filter and reveals the list; any other repo, and every branch, rewrites the palette query to the
    `repo:<owner/name>` or `branch:<name>` facet, which matches that exact value (case-insensitively), leaving
    that repo's or branch's sessions to pick from. Facets combine with each other and with free text. Branches
    are not checked out from the palette. **Start-on-branch shipped (D-19):** each branch a live session has on
    a repo also offers **New session on <repo>@<branch>** in the Branches section, which opens the dispatch bar
    with that repo and branch chosen; nothing is created until it is sent (a `branch_switch_failed` 409 shows
    inline and keeps the draft). Kiosk offers no such entry.
  - **Opening a live session from the palette clears list filters or search that would hide it,** so the list
    beside the open session shows it. Pre-spawn sessions (queued, awaiting approval) are the exception; they are
    left as they are.
  - **Phone:** the palette is keyboard-only (⌘K / Ctrl+K from a hardware keyboard). There is no touch opener. At 390 px the panel fits the screen and hides chords.
  - **Deviation:** `settingsMeta.ts` holds id, group, label, description and keywords but **not `icon`**. The icon
    stays with the page half in `settingsDefs.ts`, so the palette chunk imports no icon set. No palette row
    shows an icon.
- **Keyboard** `[A-D20]`: all bindings are registered in `shell/shortcuts.ts`, and each is checked against
  browser-reserved chords (Chrome on Linux/macOS/Windows) in a test.

| Chord | Action |
|---|---|
| Alt+Shift+M / I / E (⌥⇧ on Apple) | mode / model / effort menus. Desktop's ⌘⇧M/I/E is browser-reserved (Ctrl+Shift+I DevTools, ⌘/Ctrl+Shift+M profile menu), so this is the fallback chord; matched on `e.code`. |
| ⌘⌥↑ / ⌘⌥↓ (Alt+↑/↓ on Linux/Windows) | jump to previous / next own prompt. On Chrome Mac ⌘⌥↑/↓ shadows pane focus, which is not reserved, so the page may take it. It is chosen over ⌥↑/↓ because that one moves the caret in a textarea. Never ⌘⌥←/→, which is Chrome Mac's previous/next tab. |
| ⌘⌥[ / ⌘⌥] (Alt+[ / Alt+] on Linux/Windows) | previous / next session in list order. A bare ⌥ chord types on Mac non-US layouts (German ⌥5/⌥6 are `[` `]`), so Apple adds ⌘. |
| ⌘⌥1–9 (Alt+Shift+1–9 on Linux/Windows) | jump to the nth session in list order, from anywhere outside a non-prompt text field (the composer and dispatch prompt included). Not bare Alt+1–9: Chrome and Firefox on Linux use it to select tabs, and taking it would break tab switching. **Presets have no chord (D-14):** every digit family is taken or unsafe (⌘/Ctrl+1–9 browser tabs, Alt+1–9 Linux tabs, this jump, ⌘⇧3/4/5 macOS screenshots). The ⌘K palette's "New session from preset: <name>" entries are the keyboard path. |
| ⌘K (Ctrl+K on Linux/Windows) | palette; pressing it again closes it. Matched on `e.key` `k`, falling back to the `KeyK` code only for a non-Latin layout, so Dvorak's physical K does not open it. Not Ctrl+Shift+K (Firefox console). |
| Ctrl+O (⌘O on Apple) | cycle transcript view mode |
| Esc | stop while busy (§3) |

## 8. Configuration surfaces

| Surface | Target | Ref |
|---|---|---|
| Memory files | The Context card lists every loaded file. **Settings › Claude config › Memory** edits the **user-level** `CLAUDE.md` and syncs it across account config dirs and hosts. Repo `CLAUDE.md` is read-only in the UI; changes go through the agent and a PR. **As built (D-13):** the bug this fixed was that the CLI reads the user file only from `$CLAUDE_CONFIG_DIR/CLAUDE.md`, and no account had one, so `~/.claude/CLAUDE.md` was never loaded as *User* memory. It loaded only by accident, as a *Project* file through the walk up from HOME, and a repo outside HOME got no global instructions at all. Now `CLAUDE.md` is in the config-dir share allowlist: every account's `CLAUDE.md` links to the one `~/.claude/CLAUDE.md`, and a worker host sees it over sshfs. A private `CLAUDE.md` already in an account dir is kept and not linked, logged at INFO. The link is made even while the target is missing, because the CLI treats a dangling link as absent. Stateless helpers keep `setting_sources=[]` and still load none. The editor reads and writes that one file on the edge, with optimistic concurrency: the etag is the sha256 of the bytes, or `""` for a missing file, and Save creates it. The write is atomic (temp file in the same directory, fsync, rename, mode 0644, directory fsync), with a config auto-snapshot first and an audit row (`settings.memory_saved`). The cap is 256 KiB. A conflict shows the current text beside the kept draft, with **Reload** (take theirs) and **Keep my draft** (adopt their etag, so the next Save overwrites knowingly). A link, an oversize file or non-UTF-8 bytes are explained with no editor. A save shows the propagation note, because a worker can take up to 10 min to see it through its sshfs attribute cache (#3246). The copy reads "Applies to new sessions. Running sessions keep the instructions they started with." A dirty draft blocks navigation through the Settings unsaved-changes guard. | `[A-D8]` |
| Plugins | Settings › Plugins gains marketplaces (add/remove), browse, install and update. **As built (D-16c), owner D1-D3:** **D1 (what can be installed):** only curated marketplaces and the reviewed Directory (`anthropic-plugin-directory`, `reviewed: true` only for its entries). `command` plugin sources and `headersHelper` are refused. **The curated set is a ViberPit-owned record** (`app_settings.curated_marketplaces`, migration `0104`) of `{name: confirmed source}`, with the source normalized by `plugin_policy.normalize_source` (it mirrors the CLI parser). It is never seeded and never inferred from `known_marketplaces.json`, because a session can write that file or run `claude plugin marketplace add`. A marketplace configured outside ViberPit (seeded, or added by an agent) is listed as **unconfirmed** and cannot be browsed or installed from until the owner confirms it in Settings; the confirm shows the source and echoes it back (`POST /library/marketplaces/{name}/confirm`). A configured source that no longer equals the confirmed one is `source_changed` and is refused until re-confirmed. **Every host re-runs the policy against its own state:** the fan-out RPC carries the curated record, and `plugin_cli.host_policy` checks the host's own `known_marketplaces.json`, catalog and Directory cache, so a worker whose marketplace was re-pointed refuses instead of installing. The edge check alone is not trusted. **D2 (fan-out):** install, update, uninstall, enable and disable, and marketplace add, update and remove run on every registered host through `FleetOrchestrator.plugin_op_everywhere`, concurrently. The response is `{op, ok, hosts: [{host, outcome: ok|noop|failed, failureCode?, message?, version?}]}`. A partial failure is still HTTP 200 with per-host outcomes; nothing is rolled back and a retry is idempotent (`noop` counts as success). A host the registry marks unreachable is reported `host_unreachable` without being dialled. **D3:** the CLI's own auto-update default is kept (official marketplaces auto-update on startup); the UI shows the version and "auto-updates". **Single writer:** `server/library/plugin_cli.py` is the only code that changes plugin state, by spawning the installed `claude plugin ...` with `CLAUDE_CONFIG_DIR=shared_base()`. The direct `settings.json` writer `mutate.set_plugin_enabled` is deleted (a test fails on any other `enabledPlugins` writer). The result line is found by `CLAUDE_CODE_RESULT_NONCE` (for a `command` source the CLI prints its human prompt to stdout before the JSON line, so the first line is not reliable; an older CLI that echoes no nonce falls back to the first line), and a line with another nonce is `result_nonce_mismatch`. `-y`, `--accept-command` and every other consent-bypass flag are never passed (`assert_safe_argv`), and any gate `failureCode` refuses the run. Uninstall always passes `--keep-data`. **Consent copy (verbatim, shown on every install and update):** "Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust." After a success: "Running sessions pick this up when they restart." The server also requires `confirm: true`. **Kill switch:** `VIBERPIT_PLUGIN_INSTALL_ENABLED` (default true, listed on About). Off, the add, confirm, install, update and marketplace-update routes answer 404 `plugin_install_disabled`, browse returns nothing, and `installEnabled: false` hides those controls; toggle, uninstall and remove stay. **Human gate:** every plugin and marketplace route requires a human sender and refuses `VIBERPIT_RULES_TRUST_LOCAL_HOST` (`require_human(..., trust_local_host=False)`), because an install fans out to every host. **SSRF policy** for a marketplace source (GitHub `owner/repo` or an https git URL): IP literals in any spelling, `localhost` and `*.localhost` are refused, as is any host that does not resolve or resolves to a private, loopback, link-local, CGNAT, multicast, unspecified or reserved address. DNS rebinding between the check and the CLI's fetch is closed for marketplace add and update: the check's resolved addresses are pinned into the clone with a per-run `GIT_CONFIG_GLOBAL` (`[include] path = ~/.gitconfig` plus `[http] curloptResolve`), and update re-runs the check on the confirmed host first. Residual: a plugin entry's own git source and the CLI's auto-update clones are not pinned, and a redirect to another host is not. **Dead route deleted:** `GET /library/marketplace` (#596), which nothing called, is gone. **Live reload (built, #3314):** `POST /library/plugins/reload {holdOnCacheImpact?: bool = true}` sends the CLI's `reload_plugins` control request to every live Claude session on every host and answers `{op: "reload_plugins", ok, sessions: [{host, sessionId?, name?, outcome: applied|held|busy|skipped|failed, failureCode?, message?, plugins?, cacheImpact?}]}` (audit `library_plugins_reloaded`). It is opt-in: the consent card has an "Also reload running sessions now" checkbox (default off) and Settings › Plugins has a "Reload running sessions" button. By default a session whose tool list would change while its prompt cache depends on it is `held` and keeps its plugins; a session mid-turn is `busy`. Without the push, an install affects new and restarted sessions only. **Directory installs are verified live:** the install and update gate also fetches the listing from the Anthropic Directory API and refuses `PLUGIN_UNREVIEWED` (reason `directory_mismatch`) unless the live review state is `done` and the live source matches the cache's; an unreachable API is 503 `PLUGIN_DIRECTORY_UNAVAILABLE`. Probe record: `2026-11-05-d16c-residuals-verification.md`. Security: see `docs/product/features/plugin-marketplace.md`. | `[A-D15]` |
| MCP | Per-tool enable/disable per server (Settings) and per session (Context card). **As built (D-16b):** Claude only, gated on `flagSettings` and the MCP-scope kill switch. **Mechanism:** a disabled tool is a flag-layer `permissions.deny` rule `mcp__<server>__<tool>`, applied live through `apply_flag_settings` after `initialize` and before the first prompt (CLI start, resume, daemon restart, rotation and `/clear` all go through `start()`/`reconnect()`). Tool denies are **never** put in the spawn `--settings`: a spawn deny is a floor a session could not lift, and D4 rules the switch live and overridable. **Single composer rule:** `flag_settings.compose_permissions` is the only owner of the flag `permissions` key, and `ClaudeAgentDriver._send_permissions` is the only sender; an AST guard test (`test_only_the_composer_path_sends_a_flag_permissions_key`) fails on any other `apply_flag_settings` call carrying `permissions`. A future `/add-dir` (`additionalDirectories`) must go through the composer, because each write replaces the whole object. **Effective set:** (Settings-default-disabled minus session-enabled) plus session-disabled. A server that is off keeps its tool denies, so re-enabling it has no window with tools exposed and a server toggle never resends. **Names** are normalised as the CLI does (`[^A-Za-z0-9_-]` to `_`; for `claude.ai ` connectors also collapse `_+` and trim edge underscores); `server_for_tool` and the rule builder share one function, and a contract test against the installed binary proves every rule names a real pool entry. **Applied vs desired:** the driver keeps the rules the CLI accepted separately from the desired rules, and the payload's `tools[].enabled` shows the applied ones. A start-time apply is retried once; if it still fails, or a live write times out and the resend fails, every server serves `toolsEnforced: false` (a WARNING for the start failure, an `MCP_TOOLS_NOT_APPLIED` error event) and the UI shows "Tool settings not applied — restart the session". The next successful compose restores it. **Concurrency:** the override write, the apply and any compensation run inside one per-driver lock; a failure restores the replaced override, and cancellation-safety goes through `server/aio.py:run_to_completion`, so the DB, the CLI and the payload converge. **Human gate:** both routes require a human sender in both directions (enabling widens the tool surface; tightening is the same gate for simplicity). **Settings defaults** are stored on the existing `mcp_server_scope` row (`tool_defaults`) and take effect at the next CLI start, like server-level defaults; running sessions are not pushed (fan-out is a follow-up). There is **no repo level**, by ruling (YAGNI; add a repo dimension to the same store if needed). Session overrides live in `sessions.mcp_tool_overrides`. **Tool-list sources:** the live `mcp_status` for a session, and for Settings the last-seen list persisted from the probe and from live polls (`mcp_server_status.tools`, `tools_seen_at`), capped at 200 tools per server and 300-character descriptions; a never-seen server shows "Test the server to load its tools", and `tools: null` (Codex, a kill switch, a count-only reply) shows "Tool list unavailable". Migration `0103`. | `[A-D15]` |
| Output styles | Settings list and default; per-session picker in the `+` menu. **As built (D-16a):** Claude only, gated on `flagSettings`. **Per session:** a nullable `sessions.output_style` column (migration `0102`); the `+` menu's **Style ›** submenu lists the CLI's `availableOutputStyles` (built-ins, user, project and `plugin:Name` styles, in CLI order) and posts `POST /sessions/{id}/output-style {style}`, which applies live through `apply_flag_settings` and writes a `session.output_style` audit row. **Default:** `AppSettings.default_output_style` (`defaultOutputStyle` in `/settings`), delivered through the flag layer (D5), not by writing the shared `settings.json`, so the owner's terminal `claude` is untouched; `null` means the CLI's `default`. **Validation:** the name must be in the session's `available_output_styles` (409 `OUTPUT_STYLE_UNKNOWN`); a stored or default style the list no longer holds falls back to `default`, and a session with no list yet (detached, Codex, the Assistant drawer, kill switch) refuses with `OUTPUT_STYLE_UNAVAILABLE` and hides the picker. **Explicit own-`default`:** a session whose own stored choice is `default` sends `outputStyle: "default"`, which beats a non-default app default and a repo project style; `NULL` with no app default sends nothing, so a repo style applies. **Shared styles:** `output-styles/` joins the config-dir share allowlist, so a user style is one file visible to every account (an account's real private dir is kept, logged at INFO). **Settings › Claude config › Output styles** picks the default and lists built-in and shared user styles (no plugin styles: the edge has no live session) with a read-only viewer; there is no style editor yet. Changing style mid-session re-sends the system prompt, so the next turn misses the prompt cache (the picker says so). | `[A-D15]` |
| App slash commands | `/fork /rewind /btw /plan /effort /fast /model` are added to `server/sessions/commands/catalog.py` and dispatch to the same actions as the UI. **`/btw` (D-12)** is coding-session only; `build_catalog(kind=…)` keeps it out of the Assistant's own composer. `/btw` alone opens the drawer. `/btw <q>` opens it and sends `<q>` once through the normal send path, queueing behind a running turn and waiting for a drawer session that is still being created. The parked question stays tied to the session it was typed in: if a different session is open when it would be sent, it is dropped with a toast, and closing the drawer drops it. With the Assistant off in Settings, `/btw` toasts "The Assistant is off — turn it on in Settings" and sends nothing. With `VIBERPIT_ASSISTANT_ENABLED` off, the catalog omits it and the CLI's own `/btw` passes through. A `/btw` that reaches the server as a first prompt gets a notice. **`/fast` and `/effort` (D-14)** are app commands gated on `flagSettings`, and both are in `APP_ONLY`, so the CLI's own copies, which change state behind the record, never pass through. `/fast [on\|off]` is a `client` command: the browser posts the same toggle as ⚡, and it is listed only where fast mode is available. `/effort <level\|default>` is a `control` command that runs the same `set_model(effort=…)` action as the effort ▾; on a detached session it saves the level for the next spawn. **`/plan` and `/output-style` (D-16a)** are `control` commands and both are in `APP_ONLY`. `/plan [on|off]` (bare toggles; D6: no description argument) runs the mode picker's `set_permission_mode` path and is listed on both engines; `/plan off` when not in plan, or `/plan on` when already in plan, returns a no-op notice ("Plan mode is already off/on") with no state change and no audit row, and on a detached session it refuses with a notice, because plan mode is live driver state with nothing to persist. `/output-style <name>` runs `set_output_style`; bare, it returns a notice listing the styles. **APP_ONLY interception sits at the single `send_prompt` choke point** (`SessionOrchestrator.send_prompt`, all paths: WS composer, REST prompt, create-time first prompt, queue drain), so an app-only command is never forwarded to the CLI, which would change state behind the record. **Human-only gate:** `/plan`, `/fast`, `/effort` and `/output-style`, typed in a prompt or sent as a command frame, run only for a human sender, decided by one helper, `human_sender` in `server/auth/identity.py`, shared by REST and WS. Automated, token or unattested-local senders are refused with the notice "Slash commands from automation are not run: /plan", logged at INFO, never executed and never forwarded. A browser on the edge host itself is refused too unless `VIBERPIT_RULES_TRUST_LOCAL_HOST` is set (as for always-allow rules); the UI controls (mode picker, ⚡) still work there. A refused composer command returns its text to the box (`returnedPrompt`), so nothing typed is lost. **Create-time commands:** an APP_ONLY command in a new session's first prompt is parked with `initial_command_identity` and run once the session is live; that column is cleared when the command is consumed (run or refused), so a restart or rehydrate never replays it. `/add-dir` is out of scope (not in D15). Follow-up #3291: the existing permission-mode route has no human gate. | `[A-D15]` |
| Presets | The dispatch bar's `+` menu selects existing presets and templates, which today have no consumer. ⌘⌥1–9 shipped as the session jump (D-10, §7), so a preset chord is still to be chosen. **As built (D-14):** it reuses `SessionPreset`/`PromptTemplate` and their APIs, with no new model. Choosing a preset shows a removable chip and fills the bar's visible picks: the repo; the account and model **only if the bar offers them for the current engine** (otherwise the chip says "preset account/model unavailable" and no hidden value is sent); and the prompt when none is typed. The next `POST /sessions` carries every preset field **explicitly** from the bar's state: repo, account and model as the bar shows them, and permission mode and profile from the preset (the bar has no control for those). It **never sends `presetId`**, because the server would then fill from the preset anything the bar left absent, such as an "unavailable" account or a repo the user cleared. The Assistant's `create_session(preset=…)` keeps `presetId`. A model the account can't use is refused by the existing create refusal path. A template is inserted at the caret, rendered by `POST /templates/{id}/render` with its variable defaults. ⌘K lists `New session from preset: <name>`, which opens the bar with the chip set. **No chord** (§7). `branch_strategy` stays stored and unread. | `[A-D16]` |
| Worktrees | Settings › Sessions gains worktree location and branch prefix (today server-only). **As built (D-14):** the existing `repoRoot` setting **moved** from Settings › General to Settings › Sessions as "Worktree location". There is one editor, because `settingsMeta` has no cross-link. Copy: "Where repos are cloned and session worktrees are created. Changing it doesn't move existing worktrees — they keep working but cleanup skips them." `PUT /settings` now clears it on an explicit `null` (Reset). It was silently ignored before. **Branch prefix deferred (owner):** it stays the fixed `viberpit/`, shown read-only with "Fixed — it guards automatic branch cleanup." `brand.is_session_branch` gates `git branch -D`, so a configurable prefix needs validation and a history of every prefix ever used first. | `[A-D16]` |
| Sandbox | A session default plus a per-session toggle (Environment card). **Deferred by the owner (2026-11-04):** it fails open on hosts without socat, auto-allows Bash past permission cards, and blocks the sockets gh/push need (verification spike §2). | `[A-D16]` |
| PR auto-fix | A per-session toggle mapped onto the existing PR Doctor / autopilot, with no new mechanism. **As built (D-14):** its own nullable column `sessions.pr_autofix_paused`, kept apart from `autopilot_paused`. PR Doctor reads it from the DB row at its one decision point (`_pr_doctor_step`), so a sweep with no live driver honours it. A paused PR is skipped (INFO log, one deduped `skipped` event); alerts and pushes still fire, and manual Fix PR still runs. The pause ends when the session is archived, and it holds the backlog runner's hand-off. The Environment card's "PR auto-fix" switch shows when the session has a PR, with the copy "PR Doctor fixes failing checks and conflicts on this session's PRs". `POST /sessions/{id}/pr-autofix {enabled}` → `{enabled}`. | `[A-D16]` |
| Dev-server preview | **Shipped (D-18).** The Browser tab lists the dev servers a session started and opens one on the display the viewer can reach. **Owner decision (2026-11-05), per-click routing.** The display is chosen at click time from the viewer's verified tailnet node: `local-link` (the detected address) when the viewer is on the session's host; `tailnet-link` (the host's tailnet IP) for `bind: all`; otherwise `vnc`, on a virtual display started on demand, including for native sessions. The spawn-time `display_mode` is unchanged. **Detection.** It runs on the owning daemon through the `dev_servers` RPC; the `/proc` scan reuses `memory_watch`. Attribution: the session's own subtree; outside it, only a realpath cwd inside the session's exact managed-worktree root counts. It skips ports below 1024 and MCP/debug ports, and keeps only listeners that pass an HTML probe after attribution. The scan has a 5 s timeout. Kill switch `VIBERPIT_DEV_SERVER_DETECT_ENABLED`. **Preview window lifecycle.** Each Open replaces the previous preview, killed with a start-time-checked `terminate_process_tree`. Opens are serialised per session. A native session's preview display is released when its window closes or after 30 minutes unwatched. **Security.** `POST …/open` is human-gated and audited, and covered by the Origin allowlist from #3324. The URL is built server-side only, and the web checks it again with `isSafeHttpUrl`. Related fixes in #3324: the cross-site WebSocket hijacking hole, `browser/open` argv injection, and `/ws/vnc` routing to the owning host. **Follow-ups (not built).** Proxied preview (with isolated origins); element select via Chrome's picker over CDP; Docker-published ports; a guard on in-flight scans; #3325. | `[A-D19]` |
| Move to host | **Shipped (D-17).** `Move to host…` in the session menu opens a dialog; the session id, transcript and worktree path are kept. **Predicate** (`server/sessions/session_move.py:movable`, pure; the edge and the source daemon both run it, and the daemon re-checks inside the session's swap slot). Refusal reason codes (`MOVE_*`): `DISABLED`, `NO_FLEET`, `NOT_FOUND`, `ENGINE_UNSUPPORTED` (Codex), `ASSISTANT`, `NATIVE_DISPLAY`, `IN_FLIGHT`, `INACTIVE`, `NOT_STARTED` (queued or pending approval), `MID_TURN`, `AWAITING_ANSWER` (an open question, permission or plan card cannot be answered across a move), `HAS_CHILDREN` and `IS_CHILD` (D5), `NOT_MANAGED_WORKTREE`, `UNKNOWN_HOST`, `SAME_HOST`, `TARGET_DISABLED` / `_UNHEALTHY` / `_ENGINE` / `_PLATFORM` / `_FULL`, `DIRTY_NEEDS_CONSENT`, `DIRTY_PUBLIC_REPO`, `BACKGROUND_NEEDS_CONFIRM`, `SOURCE_UNREACHABLE`. Saga failures after the claim: `EXPORT_FAILED`, `RESTAMP_FAILED`, `IMPORT_FAILED`, `TRANSCRIPT_STALE`, `RESUME_FAILED`, `LEASE_HELD`, `PATH_MISMATCH`. **Saga** (run on the edge under `run_to_completion`): (1) predicate; (2) **claim**, a compare-and-set on `sessions.moving_to`; (3) **export** on the source (re-check, close the driver, push the branch and, with consent, a WIP commit under a hidden `refs/viberpit/transfer/*` ref, return the manifest); (4) **restamp** `host`, `moved_from`, `moved_at`; (5) **import** on the target (fetch, recreate the worktree at the identical path, verify the transcript, claim the lease with `adopt=True`, resume, delete the transfer refs); (6) **finalize** on the source (soft-reset the WIP commit, take the recovery snapshot ref, remove the worktree); (7) **restore** on any failure after export: restamp back and re-import locally on the source, outcome `restored`. New nullable columns on `sessions` (migration `0106_session_move`): `moving_to`, `move_started_at`, `moved_from`, `moved_at`, `move_imported_by`, `move_manifest` (JSON). **Trust rule:** the import-complete marker (`move_imported_by == target`, written as the true last step of the target's import) is the only signal that the target has the session. A lease alone is not enough: a target can take the lease and then fail its import and abandon its tree, so finalizing the source on a lease would destroy the only copy. **Recovery.** A claim no live saga of this edge is running is reconciled by `reconcile_loop`: a claim older than the edge's own start time can belong to no live saga and is reconciled on the first pass, so an edge restart mid-saga settles at once (deploys restart the edge often); any other claim after `MOVE_STALE_S` (10 min). A host holding a fresh lease with the marker wins; a lease without the marker is an import still in progress and is waited on up to `IMPORT_STALL_S` (the stall bound, equal to `MOVE_STALE_S`), after which the target is verified locally from the persisted manifest and kept only if it passes, else the source resumes. The loser's copy is finalized only after the winner is running. One WARNING per stuck episode per session, INFO after. **Transcript.** The target compares the transcript's size and last entry uuid with the source's manifest, reading to EOF with retries, and refuses with `MOVE_TRANSCRIPT_STALE` rather than resume from a stale file. This covers rocky's sshfs attribute cache (600 s), which can serve a stale size. **Owner decisions (2026-11-05).** **D1 (revised): uncommitted work moves as a WIP commit pushed to a hidden GitHub ref, with the user's consent, on private repos only.** A dirty tree in a public repo, or one whose visibility cannot be determined (fail closed), is refused (`MOVE_DIRTY_PUBLIC_REPO`) even with consent, because a pushed commit stays fetchable by SHA after the ref is deleted. Visibility is read from the GitHub API (`gitrepos.repo_is_private`) and checked again inside the export. **D4:** the source worktree is removed only after the target's driver has started and a recovery snapshot ref exists; if the snapshot fails the tree stays. **Rulings.** **D2:** Codex is not movable (its history lives on the host); the menu item is inert and says so. **D3:** idle sessions only, plus a confirm that stops live background tasks (`waiting_external`), which the target re-arms from `pending_rearm`; queued sessions are refused. **D5:** a live steering parent or child is refused (forks are exempt). **D6:** manual, per session only; no automatic rebalancing. **API.** `POST /api/sessions/{id}/move` `{host, carryDirty, stopBackgroundTasks}` and `GET /api/sessions/{id}/move/preflight?host=`, both human-gated (`require_human`; the preflight is gated too because it calls the GitHub API on demand). Success is 200 `{outcome: moved\|…, finalized, recoveryRef, session}`; after the claim, errors carry `outcome` `restored`, `failed` or `moved`. A driver-building call or WS attach during a move gets 409 / error frame `SESSION_MOVING`. Kill switch `VIBERPIT_SESSION_MOVE_ENABLED` (default on); capability `crossHostTransfer`. Audit row `session.move`. **UI.** The `Moving to <host>…` chip on the card and in the header; the composer's Send reads `Moving…` and keeps the draft; the dialog lists dirty paths with the private-repo consent box, the public-repo refusal, and the background-task confirm. Any failure that may have passed the claim (a 5xx, a proxy 502, a network error) shows an **outcome unknown** state with no retry, rather than a refusal. A `SESSION_MOVING` error frame arms a bounded re-attach (5 s, 11 min window) in `ws/client.ts`. **Follow-ups (not built).** A *Drain host* action (move every idle session off a host). Dead-host adoption (#949). Codex as a new thread. A manual clear for a claim stuck on a host removed from the fleet for good: restamp the row to a live host that has the worktree, or archive it, then null `moving_to`, `move_started_at`, `move_imported_by` (a product affordance belongs with #949). Org rulesets on private repos are not yet verified against the hidden-ref push (`cairnea/KinDash`). | `[A-D19]` |

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
| `GET /sessions/{id}/files/search?q=&regex=&limit=` (worktree content search, `git grep` on the owning daemon; every returned line re-read through the confined fd) — **shipped (D-20)** | Files tab "Contents" search |
| `GET /sessions/{id}/recovery-refs` (a session's recovery snapshots, live or archived, read on the owning daemon from the worktree or the clone; only well-formed refs, each with a quoted restore command) — **shipped (D-21)** | archived reader |
| `GET /github/branches?repo=` (existing; now cached 60 s per repo, stale-on-error up to an hour, bounded) — **used by ⌘K (D-21)** | palette start-on-branch |
| `GET /sessions/{id}/diff` (aggregate worktree diff vs base, structured hunks) | Changes tab, chip, rail, inline diffs |
| `POST /sessions/{id}/review-comments` (batch → one prompt) | not needed — client-composed prompt (D-5) |
| `POST /sessions/{id}/fork` `{at_message_id}` — **shipped (D-8)** as `{atTurnId, mode, prefill?, requestId}` | Fork |
| `POST /sessions/{id}/rewind` `{to_message_id}` + `POST …/rewind/undo` — **shipped (D-9)** as `POST /sessions/{id}/rewind` `{toTurnId, mode: "files_and_conversation" \| "conversation", dryRun?, requestId}` → dry run `{files, deleted, insertions, deletions, checkpointAvailable}`, real run `{rewindId, prefill, filesRestored}`; and `POST …/rewind/undo` `{rewindId}` → `{ok, skipped}`. 404 for an unknown session, 409 `{code, message}` with a `REWIND_CODES` code, 409 `daemon_outdated` | Rewind, Edit & resend, Rewind & retry |
| live `set_permission_mode` / effort / fast on the driver — **shipped (D-14)** for effort and fast as one driver method, `apply_flag_settings(patch)`. It sends the CLI's `apply_flag_settings` control request over the private control channel rewind uses, awaiting every reply before the next write, since back-to-back writes read stale. Errors map to `SettingsError` codes (`SETTINGS_UNAVAILABLE/REFUSED/INVALID/FAILED`, `FAST_UNAVAILABLE`); expected refusals log at INFO. It is Claude only (`AgentCapabilities.flag_settings`) and gated by the kill switch `VIBERPIT_FLAG_SETTINGS_ENABLED`, which surfaces as `flagSettings` on the wire. A spawn carries the stored `fastMode`/`effortLevel` in the `--settings` flag layer, never the shared `settings.json`. `POST /sessions/{id}/fast {enabled}` (409 `FAST_UNAVAILABLE` / `daemon_outdated`) and `POST /sessions/{id}/pr-autofix {enabled}` run per session through the RPC, remote, fleet and facade layers | prompt row |
| `scope=session` rule creation from the card | permission card |
| per-viewer read state — **shipped (D-10)** as `last_viewed_at`, not a seq (§7): table `session_views (identity, session_id, viewed_at)`; `GET /sessions` and `GET /sessions/archived` add `unread: bool` per row for the calling identity (one batched query; a lookup failure degrades to all-read rather than failing the list); `POST /sessions/{id}/viewed` → 204, stores `max(DB now, last_turn_at)`; `POST /sessions/{id}/unread` → 204, stores just before `last_turn_at` or drops the row with no turn yet; both 404 for an unknown session. Served by the edge from the DB, so no daemon RPC | read/unread |
| `GET /sessions/{id}/search?q=&limit=` — **shipped (D-11)**: `{hits: [{turnId, ordinal, role, snippet, matchStart, matchEnd}], truncated, totalTurns}`. Every term must match, as a case-insensitive substring (the list search's `parse_terms` / `terms_match`), over user prompts and assistant text from the replay, which follows the active chain. Thinking, tool JSON and sub-agent events are excluded. `ordinal` is the turn's `?before=` cursor; a user prompt takes the next turn's ordinal, or its own open turn's when it was a mid-turn (Codex steering) prompt. `turnId` is the prompt's id for a user hit and the turn's for an assistant hit. `snippet` is ≤ 80 chars around the first term's first occurrence, with `matchStart`/`matchEnd` as UTF-16 offsets into it. Past `limit` (default 200, max 1000) the **newest** hits are kept in transcript order, and `truncated` means older matches exist. 400 `SEARCH_EMPTY`, 404 for an unknown session, 409 `daemon_outdated`; routed per session (RPC, fleet) | transcript search |
| memory-file discovery (loaded `CLAUDE.md` set) — **shipped (D-13)**: `memoryFiles: [{path, type, tokens}]` on the `contextUsage` event, from `get_context_usage()`; persisted in `sessions.memory_files` (migration `0098`), re-emitted on attach | Context card |
| `GET /sessions/{id}/memory-file?path=` — **shipped (D-13)**: `{path, type, text, truncated, editable}`, read on the owning host (per-session RPC, fleet). Allow rule: the path must equal an entry of the session's current set **exactly** (normalized, absolute, no NUL). It is realpath'd once, and the result must sit under an allowed root, each root realpath'd first: the work tree; per config dir (the session's and the shared base) `CLAUDE.md`, `rules/**` and `projects/<slug>/memory/**`; in each cwd ancestor only the walk names `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` and `.claude/rules/**`; and `/etc/claude-code`. It is then opened from `/` with `O_NOFOLLOW` on every component (`fs_beneath.open_beneath`) and must be a regular file. A *User* import outside every root is read only by its literal path, with no realpath and every component no-follow. Capped at 256 KiB (`truncated`). `editable` is true only for the shared user file, when it is a regular file. Every refusal is 404 `MEMORY_FILE_UNKNOWN`; 409 `daemon_outdated` | memory-file viewer |
| `GET /settings/memory` → `{text, etag, exists}`; `PUT /settings/memory` `{text, etag}` → `{etag, propagation}` — **shipped (D-13)**, on the edge, normal identity auth. 409 `MEMORY_CONFLICT` `{code, text, etag}`, 409 `MEMORY_FILE_IS_LINK`, 413 `MEMORY_TOO_LARGE`, 422 `MEMORY_NOT_UTF8` (both verbs refuse the last three) | Settings › Memory |
| PR tab data and mutations — **shipped (D-15)**: `GET /api/prs/{o}/{n}/{num}` adds `reviewThreads(first: 50)` as `threads`, plus `isDraft`, `autoMergeEnabled`, `mergeStateStatus`, `labels`, `humanHold`, and falls back to the session's per-branch cache (`prs_for`) before a 404 or 502; `POST /api/prs/{o}/{n}/{num}/merge` `{method: "squash"}` (422 for any other method) and `POST …/auto-merge` `{enabled}`. The mutations run `gh` on the owning host through the orchestrator facade, RPC, remote and fleet layers. Merge-now, enable and `/api/pipeline/pr/ready-merge` check `hold:human` from a live `gh pr view --json labels` read first: 409 `{code: PR_HUMAN_HOLD}`, or `PR_HUMAN_HOLD_UNKNOWN` when the read fails (fail closed). Disable skips the check. A `gh` failure returns 200 `{ok: false, error}`. | PR tab |
| output styles — **shipped (D-16a)**: `POST /sessions/{id}/output-style` `{style: string \| null}` → `{style}` (409 `OUTPUT_STYLE_UNKNOWN` / `OUTPUT_STYLE_UNAVAILABLE` / `SETTINGS_REFUSED` / `SETTINGS_FAILED`); `outputStyle` and `availableOutputStyles` on the session payload; `defaultOutputStyle` on `GET`/`PUT /settings` (422 for a name that is neither built-in nor a shared user style); `GET /settings/output-styles` → `{styles: [{name, source, description, path?}]}` and `GET /settings/output-styles/{path}` → `{name, path, content}` (read-only, `.md` files directly in the shared dir) | Style picker, Settings › Output styles |
| MCP per-tool toggles — **shipped (D-16b)**: `POST /sessions/{id}/mcp/{server}/tools/{tool}` `{enabled}` → `{servers}` (live session override; 400 `UNKNOWN_SERVER`, 403 `SESSION_CONTROL_HUMAN_ONLY`, 409 `MCP_TOOL_UNKNOWN` / `MCP_TOOLS_UNAVAILABLE` / `SETTINGS_REFUSED` / `SETTINGS_FAILED`); `PUT /library/mcp/{server}/tools/{tool}` `{enabled}` → 204 (Settings default); `tools[]` and `toolsEnforced` on `GET /sessions/{id}/mcp`, and `tools`/`toolsSeenAt` per server on `GET /library/mcp/scope` | Context card, Settings › MCP |
| plugin marketplace ops — **shipped (D-16c)**, all under `/api/library`: `GET /plugins` → `{plugins, marketplaces, installEnabled}`; `GET /plugins/browse?q=&marketplace=&offset=&limit=` → `{plugins: BrowseEntry[], total, offset, limit, installEnabled}`; `POST /marketplaces` `{source, confirm: true}`; `POST /marketplaces/{name}/confirm` `{source, confirm: true}` → `{name, confirmedSource, curation}` (edge-only record); `POST /marketplaces/{name}/update`; `DELETE /marketplaces/{name}`; `POST /plugins/{id}/install` and `/update` `{confirm: true}`; `DELETE /plugins/{id}` (`--keep-data`); `PATCH /plugins/{id}` `{enabled}`. Mutations return `PluginOpResult` with per-host outcomes. Refusals: 400 `PLUGIN_CONFIRM_REQUIRED` / `PLUGIN_MARKETPLACE_SOURCE_REFUSED` / `PLUGIN_MARKETPLACE_NAME_RESERVED`, 404 `PLUGIN_NOT_FOUND`, 409 `PLUGIN_UNREVIEWED` / `PLUGIN_MARKETPLACE_NOT_CURATED` / `PLUGIN_COMMAND_SOURCE_REFUSED` / `PLUGIN_ENTRY_SOURCE_REFUSED`, 403 `SESSION_CONTROL_HUMAN_ONLY`, 404 `plugin_install_disabled`. Audit rows `library_marketplace_added\|removed\|updated\|confirmed` and `library_plugin_installed\|updated\|uninstalled\|toggled`. New RPC `plugin_op` (`SessionOrchestrator`, `RemoteOrchestrator`, fleet `plugin_op_everywhere`). | Settings › Plugins |
| `POST /sessions/{id}/move` `{host, carryDirty, stopBackgroundTasks}` and `GET /sessions/{id}/move/preflight?host=` — **shipped (D-17)**, human-gated. Four daemon RPCs (`transfer_preflight`, `transfer_export`, `transfer_import`, `transfer_finalize`) on `SessionOrchestrator`, `RemoteOrchestrator`, the facade and `FleetOrchestrator`; `sessions` gains `moving_to`, `move_started_at`, `moved_from`, `moved_at`, `move_imported_by`, `move_manifest` (0106); `movingTo`, `movedFrom`, `movedAt` on the session wire dict. Details in §8. | Move to host |
| `GET /sessions/{id}/dev-servers` → `{host, servers: [{port, address, bind, pid, label, title?, note?, open}]}` and `POST /sessions/{id}/dev-servers/{port}/open` → `{ok, port, url}` — **shipped (D-18)**; the open is human-gated and audited. One daemon RPC, `dev_servers`, on `SessionOrchestrator`, `RemoteOrchestrator`, the facade and `FleetOrchestrator`. Details in §8. | Preview |

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
| D-15 | PR tab — **done**: checks, review threads, merge / auto-merge (merge queue read-only), the `hold:human` REST guard, "Ask session to address" (§2, §10) | PR viewer |
| D-16 | Plugins marketplace; MCP per-tool toggles; output styles; remaining app slash commands. **Sliced:** **D-16a — done**: output styles (§8), `/plan`, `/output-style` and the human-only gate on app commands. **D-16b — done**: MCP per-tool toggles (§8; D4: live after `initialize`, overridable by the session, so a per-tool deny is live and the session can re-enable). **D-16c — done**: plugin marketplace (§8; D1: curated marketplaces plus the reviewed Directory only, `command` sources and `headersHelper` refused; D2: install fans out to every host daemon with per-host outcomes; D3: the CLI's own auto-update default is kept, and the UI shows the version and "auto-updates"). **D-16 is complete** (D-16a, D-16b and D-16c are all done). Follow-up built (#3314): an opt-in live `reload_plugins` push to running sessions (§8). Controller rulings for D-16a: D5 the default style lives in `AppSettings` via the flag layer; D6 `/plan [on\|off]` only | plugins, MCP, output styles, slash commands |
| D-17 | Move session to host — **done** (§8): idle Claude sessions move between fleet hosts through a claim / export / restamp / import / finalize / restore saga with a marker-only trust rule and crash and edge-restart reconcile. Owner decisions: D1 (revised) WIP commit to a hidden GitHub ref with consent, private repos only; D4 source worktree removed only after the target started and a recovery snapshot exists. Rulings: D2 no Codex, D3 idle only plus a background-task confirm, D5 refuse a live steering parent or child, D6 manual only. Follow-ups: Drain host, #949 dead-host adoption, Codex as a new thread, a manual clear for a claim stuck on a host removed for good, org rulesets on private repos (`cairnea/KinDash`) | move session |
| D-18 | Dev-server detection + preview — **done** (§8): the Browser tab lists a session's dev servers and opens one per click as a local link, a tailnet-IP link, or on a virtual display over VNC, chosen from the viewer's verified tailnet node. Owner decision: per-click routing. No proxy, no element select. Follow-ups: proxied preview with isolated origins, element select via Chrome's picker over CDP, Docker-published ports, a guard on in-flight scans, #3325. **Sub-spec D is complete.** | preview |
| D-19 | Parity leftovers — **done** (§5, §6): error-card recovery actions by error kind (Switch model, Lower effort, Compact), worktree Discard and Repair with recovery refs, ⌘K start-on-branch; per-model effort levels landed earlier (#3269). Follow-ups: recovery refs on the archived session; pin / star sessions (no decision). | |
| D-20 | Files pane content search — **done**: a lazy "Contents" mode in the Files tab (debounced, Regex toggle, results grouped by file, ↑/↓ + Enter, click opens the viewer at the line; match text rendered as text). Server: `git grep` with no index writes or locks, caps of 200 matches / 50 files / 20 per file, a 5 s deadline (`timedOut`), files read up to the viewer cap (`truncated` past it), a cap reached in any file reported as `truncated`, paths with a symlinked component or that cannot be parsed unambiguously refused (a newline inside a filename is rebuilt, not refused), `BAD_REGEX` / `BAD_QUERY` / `SEARCH_FAILED`. Editing stays rejected (D1). | files pane |
| D-21 | D-19 follow-ups (#3333) — **done**: recovery snapshots listed on the archived session page (refs from Discard, close, drain, the cap purge and forced removal on the session's current host; a move's snapshot stays on the source host), each with its own restore command; ⌘K start-on-branch lists the focused repo's GitHub branches. Hardened on the way: recovery refs are listed only when well-formed and the restore command quotes the ref (a planted ref could otherwise carry a shell payload into a copied command, D-19 toast included). Still open in #3333: effort ordering, ignored files on Discard, refs left on a moved session's source host. | |

## 12. Success criteria

- `scripts/desktop_parity_benchmark.py` reports **Target completion ≥ 95%**. Every non-rejected row is ✅, or ◐
  with a recorded reason.
- Every new action is hidden, not disabled-and-dead, on an engine lacking the capability (vitest per action).
- Each surface in §2–§8 renders at 390, 820 and 1440px in the Playwright viewport projects, and the e2e spec
  for each changed surface is updated in the same PR.
- Replaced code is deleted in the PR that replaces it: `session.meta` strip, in-session Plan toggle,
  `transcript/DiffView.tsx`, the "Likely complete" branch.
