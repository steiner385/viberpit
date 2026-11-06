---
slug: web-dashboard-mobile
parent: operator-experience
title: Web Dashboard & Mobile Access
state: live
links:
  code:
    - web/src/App.tsx
    - web/src/shell/AppShell.tsx
    - web/src/shell/useViewport.tsx
    - web/src/features/sessions/list/SessionCard.tsx
    - web/src/features/sessions/list/DispatchBar.tsx
    - web/src/features/sessions/list/useRepoSuggestion.ts
    - web/src/api/repoCheck.ts
    - web/src/features/sessions/SessionBody.tsx
    - web/src/features/sessions/composer/Composer.tsx
    - web/src/features/sessions/composer/PlusMenu.tsx
    - web/src/features/sessions/composer/FastToggle.tsx
    - web/src/features/settings/sections/SessionDefaultsSection.tsx
    - web/src/features/sessions/pr/PrTab.tsx
    - web/src/features/palette/AppPalette.tsx
    - web/src/features/sessions/transcript/search/TranscriptSearchBar.tsx
    - web/src/features/sessions/transcript/TurnSummaryCard.tsx
    - web/src/features/assistant/useBtwCommand.ts
    - web/src/features/assistant/AssistantScope.tsx
    - web/src/shell/assistantGestures.tsx
    - server/sessions/search.py
    - web/src/session/ordering.ts
    - web/src/shell/shortcuts.ts
    - server/sessions/read_state.py
  prs: [423, 461, 498, 776, 778, 316, 313, 330, 339, 524, 566, 376, 1471]
---

# Web Dashboard & Mobile Access

Monitor accounts and sessions and steer coding work comfortably from a desktop browser or phone.

**Problem** — Running and switching between several live `claude` sessions across multiple
Max accounts from a terminal ties you to one machine. There is no way to glance at account
usage, spot a session that needs your input, or type a follow-up prompt from a phone while
away from the desk.

**Target users** — The single operator running ViberPit on their tailnet: at the desk on a
wide screen, and on a phone or tablet anywhere on the Tailscale network. A wall-mounted kiosk
display is a fourth, unattended "user" (see Modes below).

**Desired outcome** — A browser UI, reachable from any device on the tailnet (via
`tailscale serve`), that shows a dashboard of accounts and their usage, a session list with
live status, a transcript for each session, and a prompt composer — all usable one-handed on
a phone. One shell (`AppShell`) and one route table drive every device width and mode, rather
than a mobile/desktop fork with its own copy of the rules.

**Modes** — one operator wears several hats, so the nav is organized around the question each
hat is asking rather than around a per-user account
(`docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` D1, §3):

- **Developer / vibe coder** — "What needs me right now? What are my sessions and PRs doing?"
  Primary surfaces: Sessions (composer, permission cards, the Background scope) and Pipeline.
- **Product manager** — "What are we building, how does it fit together, in what order?"
  Primary surface: Roadmap (portfolio → product → capability → initiative → issues, plus the
  Inbox scope for proposals).
- **Project manager** — "What is in flight, blocked, or waiting on a human? Is delivery
  healthy?" Primary surfaces: Roadmap's Needs-you tabs and Delivery (automations, CI health).
- **Operator / admin** — "Accounts, hosts, deploy, config." Primary surface: Settings (17
  sections across 5 groups — `web/src/features/settings/settingsDefs.ts` — split variant).

These are information-architecture groupings, not accounts or permission levels — every mode
is reachable from the same five-entry nav rail at all times.

Independently of persona, the shell also has a **kiosk mode** (`?kiosk=1`) for an unattended
wall display: `web/src/shell/useViewport.tsx` reports it as a `LayoutMode`, and
`web/src/shell/AppShell.tsx`'s region-visibility matrix (`REGION_VISIBILITY`) removes the nav
rail, session list, composer and drawer entirely, refuses every `ui/Dialog`, and enlarges the
whole type scale (`--scale`) for ten-foot readability. Only the two danger-tone banners a
passer-by must act on (daemon down, every account rate-limited) stay visible.
`shell/navDefs.ts`'s `showInKiosk` flag gates which sections a kiosk URL may ever land on — the
bar is "verified read-only", not "looks glanceable" (see `docs/ACCESSIBILITY.md` §2 for the
full region/mode table). Kiosk is orthogonal to the four persona modes above: today only
Pipeline is kiosk-safe.

Independently of both, `useViewport()` also reports a width tier (`WIDTH_TIERS`:
phone/tablet/desktop/wide). It is a *finer* axis than `LayoutMode` and gates far less: master-detail
vs. single-band rendering is the `LayoutMode`'s call, made by `REGION_VISIBILITY` in `AppShell.tsx`
(`app.list` stays mounted beside an open item on `desktop`, and gives up the one band to it on
phone/tablet), and touch targets are the unconditional `min-h-target` token rather than anything
width-dependent. The non-test readers of the tier itself are
`shell/regions/NavRail.tsx` (the rail may expand only at `wide`), `shell/regions/ContextList.tsx`
(the list column is resizable only at `desktop`/`wide`) and `ui/list/CardFrame.tsx` (swipe gestures
are live only below `desktop`). See `docs/ACCESSIBILITY.md` §2 for the full region/mode
table.

**Success metrics** — (unknown — needs owner input). Observable proxies in code: session
cards reflect live status without sticking on "Working…"; layout adapts across widths via
`useViewport`'s width tiers; safe-area insets are respected on notched phones; no dropped
interrupts/answers on reconnect.

**Non-goals** — Public internet exposure (the server binds loopback and is fronted by
`tailscale serve`; public hosting/passkey auth is a later phase). Multi-user/multi-tenant
UI. Native mobile apps — this is a responsive web app.

**Notes** — Mobile ergonomics are a heavily-churned, first-class concern, not an
afterthought: dedicated PRs cover mobile-responsive account cards and session action
buttons with overflow menus (#778, #776), swipe-to-archive/delete on cards (#313, #316),
Android soft-keyboard handling and viewport shrinking (#330, #339), a mobile-reachable tab
strip with count badges (#461), safe-area insets on edge-touching elements (#524), and a
static header/footer with only the session list scrolling (#376). A test invariant locks
the mobile/desktop single-source-of-truth so the layouts can't diverge (#423,
`web/src/shell/visibilityMatrix.test.tsx`). #1471 (finish-line row 7) retired the
`Dashboard`/`DesktopShell` fork this doc used to describe in favour of the one `AppShell`
above, moved every component listed here out of `components/` into its `features/`/`shell/`
home, and replaced the ad-hoc `lib/useMediaQuery.ts` call sites with the single
`useViewport()` reader. The session transcript is a live view bridged over WebSocket, not a
raw PTY dump — there is no terminal emulator in the tree.

**Session list grouping** — the list's Group control offers None, Repo, Status, **Branch**, **Host**,
**Date** and **PR**, and the choice is remembered per browser.
- **Date** groups by last activity into Today, Yesterday, Previous 7 days and Older, in your local time.
- **PR** files a session under its most advanced PR: Open, Draft, Merged, Closed, then No PR.
- Sessions with no branch or host collect in a trailing **No branch** / **No host** group.
- Sorting is unchanged, and the Archived list keeps its own order.

**Unread sessions** — a session's title turns **bold** when a turn has finished since you last looked at
it. Read state is per viewer and stored on the server, so it follows you across devices.
- Opening a session marks it read. While it stays open on a visible tab, each new turn end marks it read
  again after 2 s; a turn that ends while the tab is hidden stays unread until you come back. The open
  session's own card is never bold.
- The card menu has **Mark as read** / **Mark as unread**. Archived cards show the same bold, and opening an
  archived session's transcript marks it read.
- A session you have never opened is never bold, so automation sessions and everything that existed before
  this shipped start out read.
- Bold complements the amber needs-you badge; it does not replace it. Screen readers hear "unread" in the
  card's name.
- Server side: `GET /api/sessions` and `GET /api/sessions/archived` carry `unread` per session for the
  caller; `POST /api/sessions/{id}/viewed` and `POST /api/sessions/{id}/unread` set it.

**⌘K palette** — ⌘K (Ctrl+K on Linux and Windows) opens a search over everything; press it again or Esc to
close it. It works while typing in the composer.
- **Sections:** Sessions (live, background, then archived), Actions (New session and the app shortcuts,
  each with its chord), Navigate, Settings, Repos and Branches. With nothing typed it shows your 8 most
  recent sessions, then Actions.
- **Keys:** ↑/↓ move, Enter runs.
- **Repos and branches drill down inside the palette.** A repo the list's Project filter offers sets that
  filter. Any other repo, and every branch, turns the query into `repo:owner/name` or `branch:name`, which
  matches that exact repo or branch, so you pick the session from there. You can type these facets
  yourself, and combine them with each other and with words.
- **Start a session on a branch.** Each branch a live session has on a repo also lists **New session on
  repo@branch**, which opens the new-session bar with that repo and branch chosen. Once the query
  names a known repo (`repo:<name>`, or `<name>@`), the palette also lists that repo's branches from
  GitHub, default branch first; nothing is fetched for free text or when the palette opens, and a
  failed fetch just leaves the live sessions' branches. Nothing is created
  until you send; if the branch can't be switched to, the bar says so and keeps your draft. The kiosk
  doesn't offer it.
- Opening a live session from the palette clears list filters or search that would hide it (except for a
  session that has not started yet).
- **Phone:** the palette needs a hardware keyboard; there is no touch button for it. It fits a 390 px
  screen. The kiosk has no palette.

**Keyboard navigation** — every chord is listed in the shortcut sheet (⌘/ or Ctrl+/), which is built from
`web/src/shell/shortcuts.ts`.

| Action | macOS | Linux / Windows |
|---|---|---|
| Open the palette | ⌘K | Ctrl+K |
| Previous / next session in list order (wraps; with none open, ] opens the first and [ the last) | ⌘⌥[ / ⌘⌥] | Alt+[ / Alt+] |
| Open the Nth session in the list | ⌘⌥1–9 | Alt+Shift+1–9 |
| Scroll to your previous / next prompt | ⌘⌥↑ / ⌘⌥↓ | Alt+↑ / Alt+↓ |
| Search this conversation | ⌘F | Ctrl+F |

- Session keys follow the Active list's current order, including its grouping, filters and search. The
  session header's previous/next arrows use the same order.
- They work from the composer and the new-session prompt, but not from other text fields.
- Why these chords: a bare ⌥ chord types characters on Mac non-US layouts (⌥5 is `[` on a German
  keyboard), so macOS adds ⌘; bare Alt+1–9 switches browser tabs on Linux and Windows, so the jump adds
  Shift; ⌥↑/↓ moves the caret in a text field, so the prompt jump uses ⌘⌥↑/↓ on macOS.
- The prompt jump moves relative to the topmost visible message, stops following new output, and leaves
  focus where it was.

**Search in a conversation** — ⌘F (Ctrl+F) opens a search bar at the top of the transcript. It searches
the whole conversation on the server, including history that is not loaded yet.
- **Where ⌘F works:** with focus in the transcript (a click there is enough), in the message box, or with
  nothing focused. Anywhere else, such as another field, the Files or Changes tab, or Settings, it is the
  browser's own find. The ⌘K palette also offers "Search this conversation".
- **Matching:** every word you type must appear in a message, ignoring case. Your prompts and the replies'
  text are searched; thinking, tool calls and sub-agent work are not.
- **Results start with the newest:** the bar jumps straight to the newest match if it is already on screen,
  and shows `N of M`. Enter or ↑ goes to the next older match, and Shift+Enter or ↓ to the next newer one;
  both wrap. When older matches exist past the result limit, the count reads `M+`.
- **Older matches:** stepping to a match above the loaded history loads the pages in between, then centres
  the match. A long reply is centred on the match itself, not the reply's middle. Scrolling, Scroll to
  bottom or sending a message stops it; nothing more than 2000 turns back is loaded.
- **Highlights:** every match is highlighted in prompts and reply text (never inside code), and the current
  one stands out. Esc closes the bar and puts focus back where it was.

**Turn summary card** — when a turn ends, a card at the bottom of the transcript says how it ended. It
replaces the old pinned "Your turn" panel above the composer.
- **Outcome:** Complete, Handed back (with the session's reason), Your turn, Turn failed, or the usage-limit
  label. Sessions waiting on a permission, plan or question, or ready for review, show their own card
  instead. While a session is still working but has asked you something, the card is the "Asked you" note.
- **Likely complete:** a handed-back session the server reads as probably finished offers **Mark complete**
  and **Not done** here. The list card's "Likely complete" pill stays.
- **Files:** the files the turn changed (up to 5, then `+K more`) with a `+N −M` total when every change
  could be counted (Codex and notebook edits list their paths only), and an **Open Changes** button.
- **Replies:** suggested replies are one tap. Screen readers hear the hand-off reason once, when it changes.

**Session rail and Details tab** — the old meta strip under the session header is gone. A
right-hand session rail (20rem, collapsible, remembered per browser) shows four cards:
- **Changes** — totals, the first 5 files, and an Open Changes button.
- **Context** — a meter matching the prompt-row ring with an "Auto-compacts soon" line, a
  "Compact now" button near the limit (when the engine supports `/compact`), the inline MCP
  server list with a Manage link, the loaded memory files (see below), always-allow rules, and
  quota.
- **Environment** — engine, account switch and Unpin, host, display mode, branch, worktree
  path, and PR pills. For a session in a managed worktree it also has **Discard…** and **Repair**
  (people only; hidden for automation or when the worktree state can't be read).
  - **Discard…** ends the session and removes its worktree. It works only while the session is idle
    and asks you to tick a confirmation. The dialog shows the uncommitted and unpushed counts and
    whether the branch will be deleted (only a branch ViberPit created is deleted; yours is kept).
    Uncommitted or unpushed work is saved to a recovery ref first, and if git can't say whether
    work would be lost, nothing is touched. **Files git ignores (such as `.env` and build output)
    are not saved.** The session is archived, and a toast that stays up lists the recovery refs and a
    copyable restore command. The refs stay listed on the archived session's page (see below).
  - **Repair** recreates a worktree that is missing or empty. It never touches one that is present.
- **Tasks** — sub-agents and background work.

When the session pane is narrower than 64rem (phones, tablets, or desktop with the assistant
drawer pushed) the cards appear in a **Details** tab instead; this follows the pane's own width,
not the screen width. A slim toolbar keeps the status, View mode, Export and the rail toggle.

**Transcript view modes and message actions** — the session toolbar shows `View: <Mode>`,
cycled with Ctrl/⌘+O:
- **Normal** uses your Settings defaults (tool detail, thinking collapsed).
- **Thinking** expands thinking blocks.
- **Verbose** expands every tool result and the thinking blocks too.

The mode is per session and resets to Normal when you open another session. Changing the mode
resets any blocks you opened or closed by hand. Ctrl+O does not fire from text fields other than
the session composer. Each turn also has a `Message actions` menu, shown on hover or focus and
always on touch; **Copy as Markdown** copies the assistant turn's prose, or your prompt, to the
clipboard.

**Fork from here** — the `Message actions` menu offers **Fork from here**, which starts a new linked child
session whose conversation is the source's, cut at that message. The source is untouched.
- **On a reply:** the fork keeps that reply and continues after it.
- **On your own message:** the fork cuts at the reply before it and prefills the child's composer with your
  message, so you can edit it before sending. Forking your first message starts a fresh session with the same
  prefill. **`/fork`** in the composer forks at the latest finished reply.
- **Files:** the child starts from the source's **current** worktree, not from that message. It gets a new
  branch `<prefix>fork-<id>-<n>` at the source's HEAD, with uncommitted and untracked files copied.
  Restoring files to a message is rewind (D-9).
- **Refusals:** a repo whose local git config uses `includeIf` is refused (`FORK_UNSAFE_CONFIG`). So is a
  turn that is still running, one the CLI has not yet written to the transcript (the fork waits a few
  seconds first), and one from before the source's last compaction, which the CLI cannot resume at (all
  `TURN_NOT_FORKABLE`). An automation running the source is **not** a refusal: forking is read-only on
  the source, and the fork is the user's own branch (see Lineage).
- **History:** the child shows the source's conversation up to the fork point straight away. Until its
  first turn it reads the source's transcript, cut after the fork point; after that it reads its own,
  which holds the same messages. A first-message fork starts empty.
- **Lineage:** a fork's `parent_session_id` is the source, so it counts toward the source's live
  [child sessions](../guides/child-sessions.md) and their `dispatch_session` cap. Otherwise it is
  your own session, not the source's child:
  - it never notifies or wakes the source when it stops;
  - the source cannot `reply_to_child` it and does not see it in `session_context`;
  - a fork waiting on you pushes like any session, not as an escalated child;
  - it is an **automation ownership boundary** ([#3230](https://github.com/steiner385/viberpit/issues/3230)):
    an automation (Backlog Runner, PR Doctor) that owns or later re-claims the source never fences the
    fork, never waits on it to prove its run stopped, and the fork's own children and forks are outside
    that run too. A fork is never registered into the source's operation; only an operation that names
    the fork itself owns it.
- **Scope:** Claude only; Codex does not fork. The kill switch `VIBERPIT_FORK_ENABLED` (default on) hides the
  menu item and `/fork`, and the server refuses with `FORK_UNAVAILABLE`. The server side is
  `POST /api/sessions/{id}/fork`, run on the host that owns the session.

**Rewind, Edit & resend, Rewind & retry** — your own messages also offer **Rewind to here** and
**Edit & resend** in the `Message actions` menu. Both take the session back in place to just before that
message.
- **Rewind to here** cuts that message and everything after it from the conversation, and puts back the files
  Claude changed since then.
- **Edit & resend** is the same rewind, then it fills the composer with the cut message so you can change it
  and send it again. If the composer already holds an unsent draft, the confirmation says it will be replaced.
- **Rewind & retry** is a button on a failed reply's error card. It rewinds to the prompt that failed and
  sends it again unchanged, through the normal send path. It is offered only on the latest prompt's own
  failure. It is not offered for a prompt with an image, a nudge, or a prompt with no text. Because the resend
  is a new prompt, this rewind can't be undone, and the confirmation says so.
- **Other error-card actions** depend on the kind of error, in a live session. A rate limit or an
  overload offers **Switch model** (opens the model picker; it never picks for you) and **Lower
  effort** (one step down from the current level; when the level isn't known it opens the effort
  picker instead). A missing model offers Switch model only and no Rewind & retry. A context
  overflow offers **Compact**, plus Rewind & retry.
- **Confirmation:** every rewind first asks. The dialog lists the files that will change, marks the ones that
  will be deleted, and shows the total `+insertions −deletions`. It always says: *Changes made by shell
  commands are not restored, and edits made outside Claude since that message are overwritten.*
- **Divider and Undo:** the cut turns are replaced by a divider, `Rewound — N files restored`, with an
  **Undo rewind** button. Undo puts back both the conversation and the files as they were just before the
  rewind. The divider goes away after your next message, or once the undo is done.
- **Undo rules:**
  - only the session's **latest** rewind can be undone, and only until the next prompt is sent;
  - undo is refused (`REWIND_UNDO_EXPIRED`) if anything happened in the session since the rewind;
  - before rewinding, ViberPit backs up every file the rewind could touch. If that backup would exceed
    **64 MiB**, the rewind is refused (`REWIND_TOO_LARGE`) and nothing changes.
- **Limits:**
  - Only files Claude changed with its Edit and Write tools are restored. Changes made by Bash commands are not.
  - Changes made outside Claude are not restored either. A file Claude also edited is overwritten with its
    state at that message.
  - The CLI keeps the newest **100** checkpoints per session, so very old messages may have none.
  - Sessions started before D-9 have no checkpoints for their earlier messages. For those, the confirmation
    says only the conversation will be rewound.
  - Both actions are disabled while Claude is working or a permission, plan or question card is waiting on you.
- **`/rewind`** in the composer opens a picker of your 20 most recent messages. Picking one runs Rewind to here.
- **Accounts:** the CLI's file checkpoints (`file-history`) are now shared across accounts, like `projects`, so
  rewind keeps working after the session rotates to another account.
- **Scope:** Claude only; Codex does not rewind. The kill switch `VIBERPIT_REWIND_ENABLED` (default on) hides all
  of these and turns off checkpointing for new spawns, and the server refuses with `REWIND_UNAVAILABLE`. The server
  side is `POST /api/sessions/{id}/rewind` and `POST /api/sessions/{id}/rewind/undo`, run on the host that owns
  the session.

**Side chat (Assistant drawer)** — inside a session, the Assistant drawer is a side chat about that session.
- **Opening:** ⌘; (Ctrl+;) opens it, as before. `/btw` in a session's composer opens it, and `/btw <question>`
  opens it and sends the question once. If the drawer's own session is still being created, the question waits
  for it; if a turn is running, it queues. `/btw` is not offered in the drawer's own composer.
- **Tied to its session:** a parked `/btw` question belongs to the session it was typed in. If another session is
  open by the time it would be sent, it is dropped with the toast "That question was about another session — ask
  again here". Closing the drawer drops it too.
- **Header:** `About: <session>` with the session's status dot. It shows the scope the server holds, taken from
  the newest started turn that named a session. While a turn runs or prompts are queued and you have moved to
  another session, it adds "Next message will be about <that session>". Outside a session, nothing changes.
- **Read-only to that session:** the Assistant can read its transcript, diff and search results, but any change
  to it (a prompt, archive, rotate…) is refused with "This chat is about session X and can't change it — ask in
  the session itself, or close the side chat", before any confirmation card appears. Other sessions can still be
  acted on. Automation actions and PR consolidation, which can reach a session indirectly, are refused while any
  session is scoped. See `docs/product/features/ai-assistant.md` § Side chat.
- **Kill switches:** with the Assistant off in Settings, `/btw` toasts "The Assistant is off — turn it on in
  Settings" and sends nothing. With `VIBERPIT_ASSISTANT_ENABLED` off, the app's `/btw` is not listed and the CLI's
  own `/btw` passes through.

**Changes tab and inline diffs** — each session has a Changes tab next to Conversation, listed when
the session's worktree differs from its base.
- **Base:** the merge-base with origin's default branch. With no recorded `origin/HEAD`, the tab
  shows uncommitted changes only.
- **Contents:** committed, uncommitted and untracked changes.
- **Controls:** a Unified/Split toggle (unified below 1024px, split from 1024px; remembered per
  browser), Hide whitespace, and Previous change / Next change.
- **Notices:** truncated, binary and too-large files are flagged.
- **Line comments:** click a line's gutter button (`Comment on line N`, or `Comment on removed line N` on
  the old side); Shift-click extends the range on the same side within a hunk. The editor saves with
  Ctrl/⌘+Enter and cancels with Esc; saved comments show inline with Edit and Delete. A comment whose line
  has moved shows at the top of its file as outdated, and is still sent with an `(outdated)` marker. A
  sticky review bar shows `N comments`, Discard all (with a confirm dialog) and Send to Claude. Send
  composes one prompt and goes through the normal prompt path (queued when busy, held while disconnected,
  never taken as the answer to a pending question); there is no separate endpoint. Drafts persist per
  session in sessionStorage. On touch the gutter glyph stays the desktop's 20px size (rows keep their height)
  and is always shown, with an invisible 44px-wide, row-high hit area. Closing an editor, a card or the review bar
  returns focus to the gutter button, the card's Edit button or the first file header.

A `+N −M` chip in the session header opens the tab. It refreshes when a turn ends and when the tab
opens, and never polls. Edit and Write diffs in the transcript use the same renderer, with syntax
highlighting loaded lazily. The server side is
`GET /api/sessions/{id}/diff?summary=1&ignoreWhitespace=1`, computed on the host that owns the
session. It is read-only (it never touches the index) and runs git hardened: textconv, ext-diff,
system config and hooks are disabled, submodules are not entered, and filter drivers defined in
the repo or worktree config are neutralised. That leaves a window where a config rewritten mid-diff
can still run a driver, and global-scope drivers (e.g. git-lfs) are trusted; the acceptEdits write
boundary (#3217) now keeps auto-approved writes out of `.git` and other protected config, though
shell-capable modes (full-auto, bypass, an approved Bash) still have same-user access by design.

**Files tab, `@` mentions and clickable paths** — a read-only view of the session's worktree, and the
ways into it from the composer and the transcript.
- **Files tab:** file-name search, content search, line numbers, lazily loaded syntax highlighting, and virtualised large files.
  Binary and truncated files show a notice. `?view=files&file=<path>&line=<n>` jumps to a line. It is
  read-only by design; edits go through the agent.
- **Content search** (**Contents** next to the name search): searches the worktree's tracked files with
  `git grep` on the session's host, as a fixed string or a regex (smart case). Results are grouped by file;
  ↑/↓ move, Enter or a click opens the file at that line. At most 200 matches across 50 files, within 5 s; a
  notice says when results are partial (a cap, a timeout, or a match past the viewer's size limit). Symlinked
  paths are never searched.
- **`@` mentions:** typing `@` in the composer opens a menu with **Files** first (worktree files,
  fuzzy-ranked on the session's host, debounced) and **Sessions** second. Picking a file inserts `@path`,
  or `@"path"` when the path contains spaces; the CLI resolves it. The menu also works inside
  slash-command arguments (for example `/review @src/a.ts`). With no worktree it shows Sessions only.
  Enter never sends while the menu is open.
- **Clickable paths:** file paths in the transcript (a `/` or a known file extension, with optional
  `:line`) open the Files tab at that line. A small menu offers **Copy path** and **Attach as @**.
  Identifiers, versions, `host:port`, Windows paths and paths outside the worktree stay copy-only, as do
  the archived reader, sub-agent page and assistant.
- **Recovery snapshots on an archived session.** When ViberPit saved work to a recovery ref before
  removing a session's worktree (Discard, close, a deploy drain, the archive cap purge, a forced
  removal or a move), the archived page lists each snapshot with its reason, short SHA and date, and
  a copyable `git restore` command for each one. It is read on the session's host, from the
  worktree if it survives and otherwise from the repo's clone. Only well-formed ViberPit refs are
  listed, and the command quotes the ref.
- **Server:** `GET /api/sessions/{id}/files?q=&limit=` and `GET /api/sessions/{id}/files/content?path=`
  are computed on the session's host. Paths are relative to the session's cwd, as the CLI resolves `@path`,
  so a session started in a subdirectory sees only the files beneath it. Reads are capped at 512 KiB,
  refuse `.git` and anything outside the cwd, and links that escape the worktree are refused. An unknown
  session is an expected refusal, logged at INFO, not filed as an auto-error.

**Memory files (Context card and Settings › Memory)** — what the agent was told, and where you change it.
- **Context card:** a **Memory** list of every memory file the CLI actually loaded for the session, in its load
  order. It comes from the CLI's own `get_context_usage().memoryFiles`, refreshed after each turn and
  re-sent on attach. Each row shows the type (User, Project, Local, Managed, Auto memory; any other type as the
  CLI sends it), the path (relative to the worktree inside it, absolute otherwise, cut from the start so the
  file name stays) and its tokens. The list is hidden when nothing is loaded; Codex reports none. Rules and
  imports carry the type of the file that loaded them.
- **Viewer:** a row opens a read-only dialog using the Files tab's frame. It is read on the session's host, and
  only for a path in that session's loaded set (`GET /api/sessions/{id}/memory-file?path=`); anything else is
  "no longer loaded or no longer readable". Reads are capped at 256 KiB. The user file adds **Edit in Settings**.
- **Settings › Claude config › Memory:** edits `~/.claude/CLAUDE.md` on the edge host, the one user memory file
  every account loads (see [config-dir isolation](config-dir-isolation.md)). "Applies to new sessions. Running
  sessions keep the instructions they started with." Saving checks the file has not changed since you opened
  it. If it has, a **Changed elsewhere** panel shows the current text beside your draft, with **Reload** (take
  theirs) and **Keep my draft** (the next Save overwrites theirs). A missing file opens empty and Save creates
  it. A file that is a link, over 256 KiB or not UTF-8 is explained and not editable here. Leaving with unsaved
  changes asks first. Until the operator runs `viberpit memory move`, a note after a save says sessions on
  other hosts can take up to 10 minutes to see it (rocky's sshfs cache, #3246). After the move the file lives
  on the coherently-mounted account store (`~/.claude/CLAUDE.md` links to it), every host sees a save at
  once, and the note is gone (`deploy/README.md` § "The shared user `CLAUDE.md` lives in the coherent store").
- **Server:** `GET`/`PUT /api/settings/memory` (`server/api/memory.py`, etag = sha256, atomic write, audit row
  `settings.memory_saved`); the viewer is `server/sessions/memory_files.py`.

**Session settings (D-14)** — the controls Desktop keeps beside the prompt, plus where worktrees go.
- **⚡ Fast mode:** a toggle in the prompt row, shown only when the session's current model is
  fast-capable (`fastModeAvailable`, from the CLI's own model list; never on Copilot accounts). Its tooltip,
  and an inline note the first time it is turned on in a browser, say: "Fast mode — faster output on the same
  model. Bills usage credits at a higher rate, outside your plan." It is off by default and disabled while
  the toggle's request is in flight. `/fast [on|off]` does the same. The session record is the truth: a
  switch to a model without fast mode turns it off. The toggle shows the stored choice, not the CLI's
  reported cooldown state (#3254).
- **Effort ▾ on Claude:** changing effort now applies to the running Claude session at once, through the
  existing model/effort route; `/effort <level|default>` does the same. The picker offers the levels the
  current model supports (the CLI's `supportedEffortLevels` for it), and the server refuses one the model
  lacks.
- **`+` menu:** the dispatch bar's `+` holds **Attach image**, **Presets ›** and **Templates ›**. The
  composer's `+` holds **Attach file** and **Slash commands**, which seeds `/` only into an empty box.
- **Presets:** choosing one shows a removable chip, fills the repo and the prompt when none is typed, and sets
  the account and model only when the bar offers them. Otherwise the chip says "preset account/model
  unavailable". The next create sends exactly what the bar shows, plus the preset's permission mode and
  profile; it never sends the preset id, so nothing the bar declined comes back from the server. A template is
  inserted at the caret, rendered by the server with its default variables. ⌘K lists **New session from
  preset: <name>**; there is no preset chord.
- **PR auto-fix:** a switch in the session's Environment card while it has a PR; off pauses PR Doctor's
  automatic fixes for that session (see [CI/CD pipeline](cicd-pipeline-merge-queue.md)).
- **Worktree location:** Settings › Sessions › Worktrees edits the root that repos are cloned into and
  session worktrees created under, moved here from General. Changing it doesn't move existing worktrees —
  they keep working, but cleanup skips them. **Reset** returns it to the server default. The branch prefix
  `viberpit/` is shown read-only: it guards automatic branch cleanup.

**Output styles and app slash commands (D-16a)** — output styles are Claude only; `/plan` works on both engines.
- **Style ›** in the composer's `+` menu lists the styles the session's CLI reports (built-ins, your own, and
  plugin styles) and switches the style for the running session. Changing style mid-session re-sends the system
  prompt, so the next turn costs a prompt-cache miss; the tooltip says so. The menu is hidden for Codex, the
  Assistant drawer and a session that has not reported its styles yet. `/output-style <name>` does the same;
  bare `/output-style` lists the styles.
- **Settings › Claude config › Output styles** sets the style new sessions start with and lists the built-in and
  shared user styles (read-only viewer). Your own styles are `.md` files in `~/.claude/output-styles/`, shared by
  every account; there is no editor yet. A session you set to `default` keeps `default` even when the app default
  is another style.
- **`/plan [on|off]`** turns plan mode on or off (bare toggles) and says so when nothing changed.
- **Only a person runs these.** `/plan`, `/fast`, `/effort` and `/output-style` run only when typed or sent by a
  signed-in person. Automation, API tokens and unattested local senders are refused with "Slash commands from
  automation are not run" and nothing is forwarded to the CLI. A browser on the server's own host is refused
  too unless `VIBERPIT_RULES_TRUST_LOCAL_HOST` is set (the mode picker and ⚡ still work). A refused command's text
  returns to the composer.

**PR tab (D-15)** — a session that has a pull request gets a **PR** tab after Changes (`?view=pr`).
- **Header:** title, `#N`, state (Open, Draft, Merged, Closed), and badges for auto-merge on, in the merge
  queue (read-only) and **Held for a human**. A line explains the merge state in plain words, for example
  that the branch is behind its base or that checks are blocking. With several PRs a picker appears,
  defaulting to the lowest-numbered open or draft one.
- **Checks** are grouped failing, pending and passing, with failing open. **Review threads** list
  unresolved first, with resolved ones collapsed and outdated ones marked; a `path:line` links to the Files
  tab when the file exists in the worktree. **Ask session to address** puts the thread in the composer and
  switches to Conversation. It never sends, and asks before replacing text you have not sent.
- **Actions:** Arm auto-merge, Disable, Merge now (squash, behind a confirmation, only when GitHub reports the
  PR clean) and Fix. A `hold:human` PR hides all but Disable; the server enforces this too (see the REST
  hold guard in [CI/CD pipeline](cicd-pipeline-merge-queue.md)).
- **Untrusted text:** comment bodies are third-party content. They render through the sanitized Markdown
  renderer with no remote image fetches, and a link shows only when it is an absolute http(s) URL.
- If GitHub can't be reached the tab says "Live details unavailable" and shows what the session last
  cached.

**Dispatch-bar prompt checks** — two fail-open helpers sit on the new-session prompt in
`DispatchBar.tsx`. The ✨ *Assess and refine* button asks `POST /api/dispatch/assess` for a
model tier and a refined prompt, shown as the "Refined:" chip with Accept / Discard.

*Wrong-repository check* — a prompt about KinDash dispatched to `steiner385/viberpit` used to
start a session in the wrong checkout. Now, once the draft is ≥ 30 characters and has been idle
for 1.5 s, the bar asks `POST /api/dispatch/repo-check` `{prompt, repo}`
(`web/src/api/repoCheck.ts`, `useRepoSuggestion.ts`); results are cached per draft text so the
same prompt is never re-asked. When the server is confident the prompt belongs elsewhere a card
in the Refined chip's visual language (`role="status"`) appears under the textarea: "Looks like
**cairnea/KinDash** work — *reason*", with **Switch to cairnea/KinDash** (selects and persists
that repo, even one not in the Repository list) and **Keep <current repo>** (dismisses for this
draft; typing new text re-arms). Enter / Create also runs the check first, waiting at most 3 s:
a positive answer *holds* the dispatch — the card is prefixed "Not created yet — " and either
button finishes the dispatch with the chosen repo — while a null, an error, or a timeout
dispatches immediately. Kill switch: `VIBERPIT_DISPATCH_REPO_CHECK_ENABLED` (default on) on the
edge; off, the endpoint answers "no suggestion" and the UI stays silent.

**Evidence** —
- Shell and layout: `web/src/shell/AppShell.tsx` (regions + mode matrix), `web/src/shell/useViewport.tsx`
  (the one layout `matchMedia` reader), `web/src/shell/routes.tsx` (the one route table —
  `web/src/App.tsx` is a 41-line entry point that mounts it).
- Session UI: `web/src/features/sessions/list/SessionCard.tsx`,
  `web/src/features/sessions/SessionBody.tsx` (transcript host),
  `web/src/features/sessions/composer/Composer.tsx`, `web/src/features/sessions/list/DispatchBar.tsx`.
- Responsiveness: `web/src/shell/useViewport.tsx`; swipe test
  `web/src/features/sessions/list/SessionCardSwipe.test.tsx`.
- The repo-root `README.md`'s `## ViberPit web` section (Tailscale-served, phone/tablet access).
  `web/README.md` is the frontend developer guide — folders, gates, conventions — with no Tailscale
  prose.
- PRs: #423 (mobile/desktop single-source invariant), #461 (mobile-reachable tab strip),
  #776/#778 (mobile-responsive session/account controls), #313/#316 (swipe-to-archive),
  #330/#339 (Android keyboard/soft-keyboard), #524 (safe-area insets), #498 (mobile "more"
  toggle in DispatchBar), #566 (session-card badge/action layout), #1471 (`AppShell` unification,
  the four modes above, `components/` retired).
