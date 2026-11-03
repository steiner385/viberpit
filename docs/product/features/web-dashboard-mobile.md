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

**Session rail and Details tab** — the old meta strip under the session header is gone. A
right-hand session rail (20rem, collapsible, remembered per browser) shows four cards:
- **Changes** — totals, the first 5 files, and an Open Changes button.
- **Context** — a meter matching the prompt-row ring with an "Auto-compacts soon" line, a
  "Compact now" button near the limit (when the engine supports `/compact`), the inline MCP
  server list with a Manage link, always-allow rules, and quota.
- **Environment** — engine, account switch and Unpin, host, display mode, branch, worktree
  path, and PR pills.
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
- **Files tab:** search, line numbers, lazily loaded syntax highlighting, and virtualised large files.
  Binary and truncated files show a notice. `?view=files&file=<path>&line=<n>` jumps to a line. It is
  read-only by design; edits go through the agent.
- **`@` mentions:** typing `@` in the composer opens a menu with **Files** first (worktree files,
  fuzzy-ranked on the session's host, debounced) and **Sessions** second. Picking a file inserts `@path`,
  or `@"path"` when the path contains spaces; the CLI resolves it. The menu also works inside
  slash-command arguments (for example `/review @src/a.ts`). With no worktree it shows Sessions only.
  Enter never sends while the menu is open.
- **Clickable paths:** file paths in the transcript (a `/` or a known file extension, with optional
  `:line`) open the Files tab at that line. A small menu offers **Copy path** and **Attach as @**.
  Identifiers, versions, `host:port`, Windows paths and paths outside the worktree stay copy-only, as do
  the archived reader, sub-agent page and assistant.
- **Server:** `GET /api/sessions/{id}/files?q=&limit=` and `GET /api/sessions/{id}/files/content?path=`
  are computed on the session's host. Paths are relative to the session's cwd, as the CLI resolves `@path`,
  so a session started in a subdirectory sees only the files beneath it. Reads are capped at 512 KiB,
  refuse `.git` and anything outside the cwd, and links that escape the worktree are refused. An unknown
  session is an expected refusal, logged at INFO, not filed as an auto-error.

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
