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

Full inventories, with an evidence tag on every item:
`2026-11-01-claude-desktop-evidence/desktop-bundle-inventory.md` (bundle and user-data forensics) and
`2026-11-01-claude-desktop-evidence/desktop-docs-inventory.md` (official docs, with source URLs).

## 3. Benchmark results

| Dimension | ViberPit score | Parity |
|---|---:|---:|
| 1 Shell & IA | 12.5 / 16 | 78% |
| 2 Transcript | 21 / 21 | 100% |
| 3 Composer | 28.5 / 29 | 98% |
| 4 Commands & config | 15 / 15 | 100% |
| 5 Settings | 6 / 8 | 75% |
| 6 User feedback | 19 / 19 | 100% |
| 7 Lifecycle & branching | 24.5 / 25 | 98% |
| 8 Code review & dev | 9.5 / 12 | 79% |
| 9 Shortcuts & a11y | 8 / 8 | 100% |
| 10 Remote & multi-device | 8 / 8 | 100% |
| **Overall (weighted)** | **152 / 161** | **94%** |
| **Target completion** (excl. rejected rows) | **149.5 / 150.5** | **99%** |

### 1 Shell & IA

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Session sidebar + search | 3 | ✅ | Active/Background/Archived scopes, server FTS |
| Group/sort by project, folder, branch, status, env, date, PR | 2 | ✅ | repo, status, branch, host (env), date, PR; folder = repo |
| Pin / star sessions | 1 | ❌ |  · *rejected for target* |
| Read / unread marking | 1 | ✅ | per-viewer bold + card menu, live and archived; no ⌘⌥U chord |
| ⌘K palette over sessions + branches | 2 | ✅ | sessions, actions, nav, settings, repos, branches; repo/branch drill down inside the palette; each branch a session has on a repo offers "New session on repo@branch", opening the dispatch bar on it (D-19; nothing created until sent; no checkout, not a Desktop feature); keyboard-only on phone |
| Split view / tiled panes | 2 | ❌ | umbrella spec non-goal · *rejected for target* |
| Pop session into own window | 1 | ◐ | every session is a URL; no layout restore · *rejected for target* |
| Archived view + unarchive | 2 | ✅ |  |
| Session rail: Changes / Context / Environment / Tasks cards | 2 | ✅ |  |

### 2 Transcript

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Streaming markdown + code highlight | 3 | ✅ | GFM, KaTeX, Mermaid |
| Collapsible tool-call groups | 3 | ✅ |  |
| View modes Normal / Verbose / Thinking (Ctrl+O) | 2 | ✅ | per session; Normal = Settings defaults |
| Thinking blocks | 2 | ✅ |  |
| Todo list rendering | 2 | ✅ |  |
| Subagent reports + drill-down | 2 | ✅ | VP has dedicated sub-agent page |
| Compaction marker | 1 | ✅ |  |
| Per-message copy as Markdown | 2 | ✅ |  |
| Clickable file paths (open / attach as context) | 2 | ✅ |  |
| In-transcript search | 1 | ✅ | ⌘F: server search over the replay, highlights, newest-first next/prev that pages back to any hit (D-11) |
| Post-turn summary card (completed / ready for review) | 1 | ✅ | transcript tail card: outcome, changed files with +/−, Open Changes, Likely complete (D-11) |

### 3 Composer

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Enter/Shift+Enter, per-session drafts | 3 | ✅ |  |
| Stop button + Esc stops the turn | 3 | ✅ |  |
| Queue while busy + Send now (steer) | 3 | ✅ | no reorder of queue |
| Attachments: paste / drag / picker | 2 | ✅ |  |
| @-file mentions | 3 | ✅ |  |
| Slash-command palette | 3 | ✅ | catalog pushed per session |
| Model picker in prompt row | 2 | ✅ |  |
| Effort picker in session | 2 | ✅ | Codex: the catalog's levels; Claude: the current model's supportedEffortLevels, live mid-session through apply_flag_settings (D-14, #3269) |
| Permission-mode picker (5 modes) in session | 3 | ✅ |  |
| Fast-mode toggle | 1 | ✅ | toggle + cost note; state shown is the stored choice (#3254) |
| Prompt history (↑/↓ + Ctrl+R search) | 1 | ◐ | ↑/↓ only · *rejected for target* |
| Suggested next prompt (Tab) | 1 | ✅ |  |
| Dictation | 1 | ✅ | server STT + Web Speech |
| Usage ring in prompt row | 1 | ✅ | context only; plan usage → D-6 |

### 4 Commands & config

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Engine slash-command passthrough | 3 | ✅ |  |
| App-handled /fork /rewind /btw /plan /effort /fast | 2 | ✅ | /help /context /clear /model /account /mcp /fork /rewind /btw (opens the side chat and sends; D-12) /effort /fast (D-14) /plan /output-style (D-16a, human senders only); /add-dir out of scope (not in D15) |
| Plugins + marketplace browse/install | 2 | ✅ | curated marketplaces + reviewed Directory; consent card; every host (D-16c) |
| Skills management | 1 | ✅ |  |
| MCP servers + per-tool toggles | 2 | ✅ | servers, OAuth, scope; per-tool switches: Settings default + live per-session override (D-16b) |
| CLAUDE.md / memory-file editor | 2 | ✅ | as decided (D8): the Context card lists every memory file the CLI loaded, with tokens, each viewable; Settings › Memory edits the user CLAUDE.md shared by every account (etag conflict, Keep my draft); repo files read-only. Claude only (Codex reports none); type labels are the CLI's (rules/imports show as User/Project); rocky sees a save after up to 10 min (#3246) (D-13) |
| Hooks editor | 1 | ✅ | Desktop has none — VP ahead |
| Output styles | 1 | ✅ | per-session picker in the + menu, Settings default and list; user styles shared across accounts; custom-style editing not yet |
| Custom commands | 1 | ✅ |  |

### 5 Settings

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| PR behaviour: auto-fix, auto-archive on merge, branch prefix | 1 | ✅ | per-session PR auto-fix switch in the Environment card (D-14) + auto-archive + autopilot; branch prefix editable in Settings › Sessions (D-22), deletion gated on recorded ownership |
| Worktree location / policy | 1 | ✅ | repo/worktree root (D-14) and branch prefix (D-22) editable in Settings › Sessions; automatic branch deletion requires a recorded-ownership row, so no prefix can make your own branches deletable |
| Default transcript view | 1 | ✅ |  |
| Notification levels per event type | 1 | ◐ | quiet hours, channels, routing rules · *rejected for target* |
| Appearance: theme, font + transcript size | 1 | ◐ | theme + widths · *rejected for target* |
| Presets / environments selectable at new session | 2 | ✅ | dispatch bar + menu (removable chip) and ⌘K "New session from preset"; no chord — every digit family is taken (D-14) |
| Sandbox settings | 1 | ❌ | built and shipped dark (D-23): opt-in per session, fail-closed, GitHub-only network, worktree-only writes, home unreadable, non-Bash tools confined; off until host blockers are fixed — AppArmor bwrap-userns-restrict breaks the CLI seccomp helper and the CLI proxy socket sits under the hidden home (#3367) |

### 6 User feedback

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Permission card: once / this session / always / deny | 3 | ✅ |  |
| AskUserQuestion card | 3 | ✅ | multi-question batching |
| Plan approval with comments | 2 | ✅ | overall + per-step |
| Per-session status (working / waiting / review-ready) | 3 | ✅ |  |
| OS / push notifications with actions | 2 | ✅ | Web Push |
| Context meter + 'auto-compacts soon' | 2 | ✅ |  |
| Plan-usage meter | 2 | ✅ | per-window quota |
| Error recovery actions (switch model, lower effort, rewind & retry) | 2 | ✅ | switch model, lower effort (step down, or picker when unknown), rewind & retry; actions by error kind (D-19) |

### 7 Lifecycle & branching

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| New session pre-flight (folder/env/model/effort) | 3 | ✅ | plus account + host |
| Worktree visible + controls (branch, enter, discard) | 2 | ✅ | branch + path; discard with recovery refs; repair (D-19) |
| Resume / continue | 3 | ✅ |  |
| Import CLI sessions | 1 | ✅ | Adopt |
| Fork session (from here) | 3 | ✅ | Claude; files start from current worktree |
| Rewind to message (with file restore, undo) | 3 | ✅ | Claude; Edit/Write only — shell changes not restored |
| Edit & resend a previous message | 2 | ✅ |  |
| Side chat scoped to session (/btw, ⌘;) | 2 | ✅ | Assistant drawer about the open session (D-12): ⌘; opens, /btw <q> opens and sends; reads its transcript/diff/search and is refused any write to it; a separate assistant thread reading through tools, not a fork of the session's own context |
| Spin off parallel / child sessions | 1 | ✅ |  |
| Archive / unarchive / auto-archive | 2 | ✅ |  |
| Rename + AI titles | 1 | ✅ |  |
| Move session to cloud / other host | 1 | ✅ | other fleet host, idle Claude and Codex sessions (D-17; Codex built 2026-11-06, #3347); the self-hosted fleet stands in for Desktop's cloud (owner, 2026-11-06, D22c) |
| Scheduled tasks / routines | 1 | ◐ | Delivery automations · *rejected for target* |

### 8 Code review & dev

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Changes pane: aggregate diff, unified/split, line comments | 3 | ✅ |  |
| Inline tool diffs | 2 | ✅ | shared DiffRenderer, highlighted; MultiEdit edits[] not rendered |
| Files pane: view + content search | 0.5 | ✅ | read-only viewer with line numbers + highlighting; content search over the worktree (D-20): git grep on the owning daemon, fixed/regex, results grouped by file, click opens the viewer at the line |
| Files pane: edit | 0.5 | ❌ | editing rejected (D1): the Files pane stays read-only; edits go through the agent · *rejected for target* |
| PR viewer: checks, threads, merge, auto-merge | 2 | ✅ | PR tab (checks, review threads, merge / auto-merge; merge queue read-only) |
| Preview browser with dev-server detection | 2 | ✅ | detection + per-viewer open (local link / tailnet IP / VNC on demand); no proxy, no element select (D-18) |
| Integrated terminal | 2 | ❌ |  · *rejected for target* |

### 9 Shortcuts & a11y

| Desktop capability | Weight | ViberPit | Notes |
|---|:-:|:-:|---|
| Shortcut registry + ⌘/ sheet | 2 | ✅ |  |
| Keyboard menus for mode / model / effort | 1 | ✅ |  |
| Session switching by keyboard | 2 | ✅ | ⌘⌥[ ] and ⌘⌥1–9 in list order (Alt+[ ] / Alt+Shift+1–9 elsewhere) |
| Jump between prompts | 1 | ✅ | ⌘⌥↑/↓ (Alt+↑/↓ elsewhere), as Desktop |
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
| 1 | Split view / tiled panes | 1 Shell & IA | 2 |
| 2 | Integrated terminal | 8 Code review & dev | 2 |
| 3 | Pin / star sessions | 1 Shell & IA | 1 |
| 4 | Sandbox settings | 5 Settings | 1 |
| 5 | Pop session into own window | 1 Shell & IA | 0.5 |
| 6 | Prompt history (↑/↓ + Ctrl+R search) | 3 Composer | 0.5 |
| 7 | Notification levels per event type | 5 Settings | 0.5 |
| 8 | Appearance: theme, font + transcript size | 5 Settings | 0.5 |
| 9 | Scheduled tasks / routines | 7 Lifecycle & branching | 0.5 |
| 10 | Files pane: edit | 8 Code review & dev | 0.5 |

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

## 6. Outcome

Each gap was reviewed with the owner, one at a time, in ranked-gap order on 2026-11-01. The decisions are in §7.
The resulting target is `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` (sub-spec D of the UI
umbrella), whose §11 sequences the work into PRs D-1 … D-18. D-19 then closed the parity leftovers: recovery
actions by error kind on the inline error card, worktree Discard and Repair on the Environment card, and
"New session on repo@branch" from ⌘K (rows marked D-19 in §5). D-20 added content search to the Files pane. The "Files pane (view / edit)" row
was split into its adopted half (view + content search, ✅) and its rejected half (edit, D1), each weight 0.5, so
the rejected half counts against overall parity but not the target.

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
| D11 | Permission card (#13) | **Four buttons:** Allow once / Allow for this session / Always allow… / Deny. A session-scoped grant appears in the session rules readout and can be revoked there. |
| D12 | Sidebar and rail (#14–17) | **Adopt:** more group/sort keys (branch, host/environment, date, PR state); read/unread; a session rail with Changes / Context / Environment / Background-task cards replacing the meta strip (collapsible on desktop, a tab on phone). **Reject:** pin/star. |
| D13 | Transcript (#18–20, #46) | **Adopt all:** in-session view modes Normal / Verbose / Thinking (Ctrl+O, with the Settings value as default); clickable file paths that open the Files viewer at the line (attach as `@` and copy as secondary actions); in-transcript search across unloaded pages; a post-turn summary card that subsumes "Likely complete". |
| D14 | Prompt row (#21–24) | **Adopt all:** model (⌘⇧I, keeping gated-model approval), effort (⌘⇧E), fast-mode toggle and usage ring in the prompt row. Model moves out of the meta strip. |
| D15 | Commands and config (#25–28) | **Adopt all:** app-handled `/fork /rewind /btw /plan /effort /fast /model` drive the same actions as the UI; plugin marketplace browse/install; MCP per-tool toggles (per server and per session); output styles. |
| D16 | Worktrees, presets, sandbox (#29–31, #35, #48) | **Adopt all:** worktree and branch shown in the Environment card (open PR / discard / repair) plus Settings for worktree location and branch prefix; presets selectable in the dispatch bar (⌘⌥1–9); sandbox default and per-session toggle; a per-session PR auto-fix toggle mapped onto PR Doctor and autopilot. |
| D17 | Feedback (#32–34) | **Adopt all:** plan approval with comments; "auto-compacts soon" plus a /compact action; error-card recovery (switch model / lower effort / rewind & retry). |
| D18 | Side chat (#36) | **Context-aware Assistant:** ⌘; inside a session opens the Assistant drawer scoped to that session (reads its transcript and diff, never writes to it). Outside a session it stays app-level. `/btw` opens the same drawer. No second chat surface. |
| D19 | Dev surfaces (#37–41) | **Adopt all:** move a session between fleet hosts; inline tool diffs rendered by the Changes-pane renderer (one renderer); a PR tab (checks, threads, merge/auto-merge, from Pipeline data); dev-server detection offered in the Browser tab or a proxied preview. |
| D20 | Keyboard (#42–44, #47) | **Adopt:** ⌘⇧M/I/E menus (each verified against browser-reserved chords); previous/next session and 1–9 jump on non-reserved chords; jump between prompts. **Reject:** Ctrl+R prompt search. |
| D21 | Leftovers (#45, #49–51) | **Reject all:** pop-out window, per-event notification levels, font/transcript size, routines in the session UI. |

### Follow-up decisions (owner review, 2026-11-06)

The three target rows left open after D-21, reviewed one at a time.

| # | Gap | Decision |
|---|---|---|
| D22a | Sandbox settings (#48) | **Adopt, opt-in and fail-closed, spike first.** The toggle is offered only on a host whose preflight passes (bubblewrap and socat present); elsewhere it is disabled with the reason. The CLI's Bash auto-allow is turned off so permission cards still appear. GitHub hosts and the gh/git sockets are allowed explicitly. Off by default, with a per-session toggle in the Environment card. Supersedes the 2026-11-04 deferral. |
| D22b | Branch prefix (#31) | **Adopt with recorded ownership.** The prefix is configurable in Settings › Sessions, and every branch ViberPit creates is recorded in the DB. Automatic deletion requires that record, not just a matching name. The built-in `viberpit/` and legacy `ccswitch/` prefixes keep matching older branches. A prefix that clashes with an existing branch namespace is refused. |
| D22c | Move session to cloud (#50) | **The fleet move counts.** The self-hosted fleet (rocky or any onboarded runner) stands in for Desktop's cloud, so D-17's move between hosts closes the row. Moving Codex sessions is tracked in #3347. |
| D22d | Sandbox amendments after the spike (`docs/superpowers/specs/2026-11-06-d23-sandbox-spike.md` §4) | **Adopt all five (owner, 2026-11-06):** (1) no unix sockets on Linux (`allowAllUnixSockets` would be an escape); gh authenticates from a file-backed `GH_CONFIG_DIR` instead of the keyring. (2) In `full-auto`/bypass the sandbox is containment only, with no cards, so `allowUnsandboxedCommands:false` is mandatory. (3) socat installed on rocky (done 2026-11-06), so both hosts can offer it. (4) Toggling the sandbox respawns the session; it is never enabled live. (5) A strict network allowlist of GitHub domains only: unknown domains are denied, not prompted. |
| D22e | Automations reached through GitHub from a sandboxed session (spike follow-up) | **Accept and document (owner, 2026-11-07):** a sandboxed session can comment or file issues; the automation session that results follows "Sandbox new sessions". |
| D22f | Sub-agent write race in sandboxed sessions | **Serialise writes against running Bash (owner, 2026-11-07):** Edit/Write are refused while any Bash runs in any thread of the session. |
| D22g | Live run found host blockers (AppArmor seccomp helper, proxy socket under home) | **Ship dark (owner, 2026-11-08):** merged with the kill switch off by default; the preflight names both blockers; enablement tracked in #3367. |

