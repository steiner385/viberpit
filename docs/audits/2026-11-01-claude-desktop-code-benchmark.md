# ViberPit web UI vs Claude Desktop "Code" mode — audit and parity benchmark

**Date:** 2026-11-01 · **Reference build:** Claude Desktop for Linux (package `claude-desktop` 4.4212.0, installed
and running on dobby) · **ViberPit:** `origin/master` @ `7ffb589d` · **Benchmark script:**
`scripts/desktop_parity_benchmark.py` (re-run after each change; section 3 is its output).

Feeds the review of `docs/superpowers/specs/2026-09-02-ui-target-state-design.md` §5 ("Claude Code Desktop
alignment"), whose governing principle 2 is "defer to Claude Code Desktop where both products have an opinion".

## 1. Method

Three independent inventories, each organised under the same ten dimensions:

1. **Desktop, from the shipped bundle** (strongest evidence). `app.asar` and `ion-dist.asar` were extracted
   read-only; the Code-mode renderer ("epitaxy"), its `LocalSessions` IPC surface (355 methods), its keybinding
   table and its English string catalogue were read. User-data files under `~/.config/Claude/` were read for
   schema only. Nothing was executed and no credentials were read.
2. **Desktop, from official docs**: code.claude.com/docs (desktop, quickstart, scheduled tasks, remote control,
   permission modes, web, mobile, checkpointing) and the Claude Code CHANGELOG.
3. **ViberPit, from code**: `web/src/` and the specs, every claim cited to `path:line`, plus screenshots of the
   live edge (`127.0.0.1:8787`).

Scoring: each Desktop capability is one row worth its weight (3 = core chat loop, 2 = important, 1 = nice).
ViberPit earns the full weight for ✅, half for ◐ and nothing for ❌. Capabilities only ViberPit has are listed in
§4 and are **not** scored, so the percentage measures parity, not overall quality.

## 2. What Desktop's Code mode is (key facts)

- **Engine:** Desktop downloads the real Claude Code CLI per version (`~/.config/Claude/claude-code/<ver>/`) and
  drives it over stream-json and the SDK control protocol (`--fork-session`, `--resume-session-at`,
  `--include-hook-events`, `--permission-prompt-tool`). It is a UI over the CLI, the same architecture as
  ViberPit's `claude_driver.py`, so most of its features are reachable by ViberPit through the same seams.
- **Shell:** a sidebar with configurable sections (pinned, routines, sessions), grouping by
  project/folder/branch/status/environment/date/PR, pin/star, read/unread, and a ⌘K palette over sessions and
  branches. A tiling pane area holds chat, Changes (diff), Files, Preview, Terminal, Tasks, Plan and Subagent
  panes; sessions can be dragged into their own window and the layout is restored on relaunch. A session rail
  carries Changes / Context / Environment / Background-task cards.
- **Transcript:** Normal / Verbose / Thinking views (Ctrl+O); per-message copy, attach, **fork from here** and
  **rewind to here**; paged loading and search run in a worker; a post-turn summary card.
- **Composer:** reorderable queued-message stack with Send now; Esc stops and double-Esc rewinds or edits the
  last message; Ctrl+R history; keyboard menus for mode / model / effort / fast (⌘⇧M/I/E); `@`-file mentions;
  environment presets (⌘⌥1–9); usage ring.
- **Permission modes:** Ask (`default`), Accept edits, Plan, Auto (classifier) and Bypass (opt-in). The card
  offers Allow once / Allow for this session / Always allow / Deny. Plan approval accepts comments.
- **Branching:** fork, rewind with file restore and undo/redo, side chats (`/btw`, ⌘;) that are tabbed,
  pinnable and floatable, worktree create/enter/discard/repair, import and live-tail of CLI sessions, move to
  cloud, SSH hosts, scheduled routines.
- **Dev surfaces:** Changes pane (unified/split, whitespace toggle, line comments, F7), editable Files pane, full
  PR viewer (checks, threads, merge, auto-merge, merge queue), Preview with dev-server detection and element
  select, and PTY terminal tabs.
- **Absent in Desktop:** usage-aware multi-account rotation, a hooks editor, fleet orchestration, and a phone-native
  UI. On a phone it relies on Remote Control through claude.ai and the mobile app.

Full inventories, with an evidence tag on every item, were kept in the session's scratch directory
(`desktop-bundle-inventory.md`, `desktop-docs-inventory.md`).

## 3. Benchmark results

| Dimension | ViberPit score | Parity |
|---|---:|---:|
| 1 Shell & IA | 7.5 / 16 | 47% |
| 2 Transcript | 15.5 / 21 | 74% |
| 3 Composer | 18.5 / 29 | 64% |
| 4 Commands & config | 9 / 15 | 60% |
| 5 Settings | 3.5 / 8 | 44% |
| 6 User feedback | 14.5 / 19 | 76% |
| 7 Lifecycle & branching | 13.5 / 25 | 54% |
| 8 Code review & dev | 3 / 12 | 25% |
| 9 Shortcuts & a11y | 5 / 8 | 62% |
| 10 Remote & multi-device | 8 / 8 | 100% |
| **Overall (weighted)** | **98 / 161** | **61%** |

### 1 Shell & IA

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Session sidebar + search | 3 | ✅ | Active/Background/Archived scopes, server FTS |
| Group/sort by project, folder, branch, status, env, date, PR | 2 | ◐ | group none/repo/status only |
| Pin / star sessions | 1 | ❌ |  |
| Read / unread marking | 1 | ❌ | needs-you badge only |
| ⌘K palette over sessions + branches | 2 | ❌ |  |
| Split view / tiled panes | 2 | ❌ | umbrella spec non-goal |
| Pop session into own window | 1 | ◐ | every session is a URL; no layout restore |
| Archived view + unarchive | 2 | ✅ |  |
| Session rail: Changes / Context / Environment / Tasks cards | 2 | ◐ | meta strip has ctx, host, bg tasks; no Changes |

### 2 Transcript

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Streaming markdown + code highlight | 3 | ✅ | GFM, KaTeX, Mermaid |
| Collapsible tool-call groups | 3 | ✅ |  |
| View modes Normal / Verbose / Thinking (Ctrl+O) | 2 | ◐ | Settings default only |
| Thinking blocks | 2 | ✅ |  |
| Todo list rendering | 2 | ✅ |  |
| Subagent reports + drill-down | 2 | ✅ | VP has dedicated sub-agent page |
| Compaction marker | 1 | ✅ |  |
| Per-message copy as Markdown | 2 | ❌ |  |
| Clickable file paths (open / attach as context) | 2 | ◐ | click-to-copy only |
| In-transcript search | 1 | ❌ |  |
| Post-turn summary card (completed / ready for review) | 1 | ◐ | 'Likely complete' panel |

### 3 Composer

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Enter/Shift+Enter, per-session drafts | 3 | ✅ |  |
| Stop button + Esc stops the turn | 3 | ◐ | Esc detaches instead of stopping |
| Queue while busy + Send now (steer) | 3 | ✅ | no reorder of queue |
| Attachments: paste / drag / picker | 2 | ✅ |  |
| @-file mentions | 3 | ❌ | @ matches session titles, assistant only |
| Slash-command palette | 3 | ✅ | catalog pushed per session |
| Model picker in prompt row | 2 | ◐ | in meta strip |
| Effort picker in session | 2 | ◐ | dispatch-time only |
| Permission-mode picker (5 modes) in session | 3 | ◐ | Plan toggle only; Settings for the rest |
| Fast-mode toggle | 1 | ❌ |  |
| Prompt history (↑/↓ + Ctrl+R search) | 1 | ◐ | ↑/↓ only |
| Suggested next prompt (Tab) | 1 | ✅ |  |
| Dictation | 1 | ✅ | server STT + Web Speech |
| Usage ring in prompt row | 1 | ❌ | UsageRing built, unused |

### 4 Commands & config

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Engine slash-command passthrough | 3 | ✅ |  |
| App-handled /fork /rewind /btw /plan /effort /fast /add-dir | 2 | ◐ | /help /context /clear /model /account /mcp |
| Plugins + marketplace browse/install | 2 | ◐ | enable/disable + detail; no marketplace |
| Skills management | 1 | ✅ |  |
| MCP servers + per-tool toggles | 2 | ◐ | servers, OAuth, scope; no per-tool toggles |
| CLAUDE.md / memory-file editor | 2 | ❌ |  |
| Hooks editor | 1 | ✅ | Desktop has none — VP ahead |
| Output styles | 1 | ❌ |  |
| Custom commands | 1 | ✅ |  |

### 5 Settings

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| PR behaviour: auto-fix, auto-archive on merge, branch prefix | 1 | ◐ | auto-archive + autopilot; no branch prefix |
| Worktree location / policy | 1 | ❌ | server-only |
| Default transcript view | 1 | ✅ |  |
| Notification levels per event type | 1 | ◐ | quiet hours, channels, routing rules |
| Appearance: theme, font + transcript size | 1 | ◐ | theme + widths |
| Presets / environments selectable at new session | 2 | ◐ | editable in Settings, not selectable at dispatch |
| Sandbox settings | 1 | ❌ |  |

### 6 User feedback

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Permission card: once / this session / always / deny | 3 | ◐ | no 'this session' scope |
| AskUserQuestion card | 3 | ✅ | multi-question batching |
| Plan approval with comments | 2 | ◐ | Approve & run / Keep planning |
| Per-session status (working / waiting / review-ready) | 3 | ✅ |  |
| OS / push notifications with actions | 2 | ✅ | Web Push |
| Context meter + 'auto-compacts soon' | 2 | ◐ | % only |
| Plan-usage meter | 2 | ✅ | per-window quota |
| Error recovery actions (switch model, lower effort, rewind & retry) | 2 | ◐ | auto-resend, rotate |

### 7 Lifecycle & branching

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| New session pre-flight (folder/env/model/effort) | 3 | ✅ | plus account + host |
| Worktree visible + controls (branch, enter, discard) | 2 | ◐ | automatic but invisible |
| Resume / continue | 3 | ✅ |  |
| Import CLI sessions | 1 | ✅ | Adopt |
| Fork session (from here) | 3 | ❌ | engine capability exists, no UI |
| Rewind to message (with file restore, undo) | 3 | ❌ |  |
| Edit & resend a previous message | 2 | ❌ |  |
| Side chat scoped to session (/btw, ⌘;) | 2 | ◐ | app-level assistant drawer |
| Spin off parallel / child sessions | 1 | ✅ |  |
| Archive / unarchive / auto-archive | 2 | ✅ |  |
| Rename + AI titles | 1 | ✅ |  |
| Move session to cloud / other host | 1 | ❌ |  |
| Scheduled tasks / routines | 1 | ◐ | Delivery automations |

### 8 Code review & dev

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Changes pane: aggregate diff, unified/split, line comments | 3 | ❌ |  |
| Inline tool diffs | 2 | ◐ | rebuilt from tool input, no highlighting |
| Files pane (view / edit) | 1 | ❌ |  |
| PR viewer: checks, threads, merge, auto-merge | 2 | ◐ | Pipeline page + PR pills + Fix |
| Preview browser with dev-server detection | 2 | ◐ | VNC Browser tab, no dev-server detect |
| Integrated terminal | 2 | ❌ |  |

### 9 Shortcuts & a11y

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Shortcut registry + ⌘/ sheet | 2 | ✅ |  |
| Keyboard menus for mode / model / effort | 1 | ❌ |  |
| Session switching by keyboard | 2 | ◐ | list arrows; no ⌘1-9 / Ctrl+Tab |
| Jump between prompts | 1 | ❌ |  |
| Accessibility (landmarks, live regions, reduced motion) | 2 | ✅ | VP ahead |

### 10 Remote & multi-device

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Use from phone | 3 | ✅ | native PWA — VP ahead |
| Push to phone | 2 | ✅ |  |
| Remote hosts (SSH / fleet) | 2 | ✅ | fleet runners |
| Hand work over from phone (Dispatch) | 1 | ✅ | dispatch bar on phone |

### Ranked gaps (weight × missing)

| # | Gap | Dimension | Points lost |
|---|---|---|---:|
| 1 | @-file mentions | 3 Composer | 3 |
| 2 | Fork session (from here) | 7 Lifecycle & branching | 3 |
| 3 | Rewind to message (with file restore, undo) | 7 Lifecycle & branching | 3 |
| 4 | Changes pane: aggregate diff, unified/split, line comments | 8 Code review & dev | 3 |
| 5 | ⌘K palette over sessions + branches | 1 Shell & IA | 2 |
| 6 | Split view / tiled panes | 1 Shell & IA | 2 |
| 7 | Per-message copy as Markdown | 2 Transcript | 2 |
| 8 | CLAUDE.md / memory-file editor | 4 Commands & config | 2 |
| 9 | Edit & resend a previous message | 7 Lifecycle & branching | 2 |
| 10 | Integrated terminal | 8 Code review & dev | 2 |
| 11 | Stop button + Esc stops the turn | 3 Composer | 1.5 |
| 12 | Permission-mode picker (5 modes) in session | 3 Composer | 1.5 |
| 13 | Permission card: once / this session / always / deny | 6 User feedback | 1.5 |
| 14 | Group/sort by project, folder, branch, status, env, date, PR | 1 Shell & IA | 1 |
| 15 | Pin / star sessions | 1 Shell & IA | 1 |
| 16 | Read / unread marking | 1 Shell & IA | 1 |
| 17 | Session rail: Changes / Context / Environment / Tasks cards | 1 Shell & IA | 1 |
| 18 | View modes Normal / Verbose / Thinking (Ctrl+O) | 2 Transcript | 1 |
| 19 | Clickable file paths (open / attach as context) | 2 Transcript | 1 |
| 20 | In-transcript search | 2 Transcript | 1 |
| 21 | Model picker in prompt row | 3 Composer | 1 |
| 22 | Effort picker in session | 3 Composer | 1 |
| 23 | Fast-mode toggle | 3 Composer | 1 |
| 24 | Usage ring in prompt row | 3 Composer | 1 |
| 25 | App-handled /fork /rewind /btw /plan /effort /fast /add-dir | 4 Commands & config | 1 |
| 26 | Plugins + marketplace browse/install | 4 Commands & config | 1 |
| 27 | MCP servers + per-tool toggles | 4 Commands & config | 1 |
| 28 | Output styles | 4 Commands & config | 1 |
| 29 | Worktree location / policy | 5 Settings | 1 |
| 30 | Presets / environments selectable at new session | 5 Settings | 1 |
| 31 | Sandbox settings | 5 Settings | 1 |
| 32 | Plan approval with comments | 6 User feedback | 1 |
| 33 | Context meter + 'auto-compacts soon' | 6 User feedback | 1 |
| 34 | Error recovery actions (switch model, lower effort, rewind & retry) | 6 User feedback | 1 |
| 35 | Worktree visible + controls (branch, enter, discard) | 7 Lifecycle & branching | 1 |
| 36 | Side chat scoped to session (/btw, ⌘;) | 7 Lifecycle & branching | 1 |
| 37 | Move session to cloud / other host | 7 Lifecycle & branching | 1 |
| 38 | Inline tool diffs | 8 Code review & dev | 1 |
| 39 | Files pane (view / edit) | 8 Code review & dev | 1 |
| 40 | PR viewer: checks, threads, merge, auto-merge | 8 Code review & dev | 1 |
| 41 | Preview browser with dev-server detection | 8 Code review & dev | 1 |
| 42 | Keyboard menus for mode / model / effort | 9 Shortcuts & a11y | 1 |
| 43 | Session switching by keyboard | 9 Shortcuts & a11y | 1 |
| 44 | Jump between prompts | 9 Shortcuts & a11y | 1 |
| 45 | Pop session into own window | 1 Shell & IA | 0.5 |
| 46 | Post-turn summary card (completed / ready for review) | 2 Transcript | 0.5 |
| 47 | Prompt history (↑/↓ + Ctrl+R search) | 3 Composer | 0.5 |
| 48 | PR behaviour: auto-fix, auto-archive on merge, branch prefix | 5 Settings | 0.5 |
| 49 | Notification levels per event type | 5 Settings | 0.5 |
| 50 | Appearance: theme, font + transcript size | 5 Settings | 0.5 |
| 51 | Scheduled tasks / routines | 7 Lifecycle & branching | 0.5 |

## 4. Where ViberPit leads (not scored)

Multi-account usage-aware rotation · Codex/Copilot engines behind one UI · fleet hosts and capacity governor ·
autopilot, backlog runner and goals · Roadmap / Pipeline / Delivery sections · VNC live browser of the agent's
own display · assistant drawer with cross-session tools · hooks editor with dry-run · config snapshots ·
notification channels and routing rules · learned quick replies · kiosk mode · phone-native PWA · WCAG 2.1 AA
landmarks and live regions.

## 5. Drift from the umbrella spec's §5

The 2026-09-02 umbrella spec already promised these Desktop-alignment items, and they **did not ship**:

| Promised in §5 | State on master |
|---|---|
| Mode labels Manual / Accept edits / Plan / Auto / Bypass | ViberPit labels remain (`web/src/session/permissionModes.ts:6-11`) |
| Permission-mode selector next to Send | `hideModeSelector` prop is accepted but ignored (`SessionComposer.tsx:34`) |
| Views menu with Browser, Tasks, PR | Conversation and Browser only (`SessionBody.tsx:77`) |
| Transcript view modes Normal / Verbose / Summary | only a Settings default |
| `UsageRing` in the composer | component built (`ui/UsageRing.tsx`), unused |
| Diff chip `+12 −1` | absent |
| `Esc` = stop | `Esc` detaches (`shell/shortcuts.ts`) |

The spec's "Not inherited now" list (pane grid, terminal, file editor, side chat, split view) accounts for only
6.5 of the 63 points lost. Most of the gap is in the core chat loop: `@`-files, fork, rewind, edit-and-resend,
the Changes pane, and the mode and permission controls.

## 6. Review order

The gaps are reviewed one at a time with the owner, in ranked-gap order. Each decision (adopt / adapt / reject,
with a reason) is recorded in the updated UI spec.

## 7. Decisions log (owner review, 2026-11-01)

| # | Gap | Decision |
|---|---|---|
| D1 | Spec non-goals (pane grid, terminal, editor, side chat, split view) | **Lift selectively:** Changes pane, read-only Files viewer and a session-scoped side chat become session tabs beside Conversation/Browser. Terminal, file editor and split view stay out. |
| D2 | `@` mentions (#1) | **Files and sessions:** one `@` menu in the session composer. Worktree files come first and are inserted as `@path`, which the CLI resolves natively; other sessions come second. Needs a per-host file-list endpoint. |
| D3 | Fork and rewind (#2, #3) | **Both, Desktop-style:** per-message "Fork from here" opens a linked child session in its own worktree; "Rewind to here" restores conversation and files, with undo. Checkpoint behaviour is verified against the real CLI first. |
| D4 | Changes pane (#4) | **Full:** aggregate worktree diff with a `+N −M` header chip, unified (phone) / split (desktop) toggle, whitespace toggle, next-change navigation, and line comments batched into one prompt. |
| D5 | ⌘K palette (#5) | **Global:** ⌘K/Ctrl+K over sessions (including archived), repos/branches, sections and app actions, driven from the existing `shortcuts`/`navDefs` registries. |
| D6 | Split view (#6), terminal (#10) | Out, per D1. |
| D7 | Per-message actions (#7, #9) | **Copy as Markdown** on every message. **Edit & resend** on user messages, implemented as rewind-to-before-message plus a prefilled composer, reusing D3 rather than a second mechanism. |
| D8 | CLAUDE.md (#8) | **View all, edit user-level:** the session Context card lists every memory file actually loaded. Settings edits the user-level file, synced across accounts and hosts. Repo CLAUDE.md is read-only in the UI. |
| D9 | Esc (#11) | **Esc stops while a turn runs** (with "Stopped · Esc" feedback). While idle, Esc detaches. ← always detaches. |
| D10 | Permission-mode picker (#12) | **Picker beside Send** (⌘⇧M), changeable mid-session. Desktop names first (Ask / Accept edits / Plan / Auto / Bypass), with Plan folded in and the separate toggle removed. Allowlist and Full auto stay as a labelled ViberPit group. Mode keys are unchanged. |
