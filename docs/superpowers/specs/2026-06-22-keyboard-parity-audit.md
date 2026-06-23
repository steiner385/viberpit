# Keyboard Parity Audit — ccswitch Web vs Claude Code

**Date:** 2026-06-22
**Status:** Report (Part C of the keyboard-nav-parity effort). Deliverable is a prioritized gap list — implementations are deferred for the user to choose from.
**Companion:** follows PR #128 (sessions-list ↑/↓ + in-session command history).

## Method & scope

Inventoried every keyboard handler in `web/src` (≈40 bindings across DispatchBar, Dashboard,
`useGlobalListNav`, SessionView, Composer, CommandPalette, PermissionCard, QuestionCard,
DesktopShell, ShortcutsHelp, SessionCard, BrowserTab), then compared against Claude Code's
interactive keyboard model. Each Claude Code binding is marked:

- **Present** — equivalent exists and behaves comparably.
- **Partial** — works but weaker than Claude Code (e.g. operable by Tab+Enter but no rapid keys).
- **Absent** — no equivalent.
- **N/A (web)** — a terminal-only binding with no meaningful browser analog, *or* handled by the
  underlying `claude` agent rather than the web layer.

"Parity" here means *a keyboard user can drive the session as fluidly as in Claude Code* — not a
literal keycode match. This is a browser chat UI, not a PTY, so some bindings map differently and
some don't apply.

## What already has parity (no action)

The web UI is already keyboard-rich. These match Claude Code well:

| Claude Code | ccswitch web | Where |
|---|---|---|
| `Enter` submit | Present | Composer / DispatchBar |
| `Shift+Enter` newline | Present | Composer / DispatchBar |
| `↑/↓` prompt history | Present (PR #128) | Composer (boundary-aware, draft-preserving) |
| `Shift+Tab` cycle permission/auto-accept/plan modes | Present | SessionView (`nextPermissionMode`) |
| `/` slash-command menu | Present | CommandPalette (app-level commands) |
| `Tab` accept completion | Present (ghost suggestion) | Composer |
| `?` shortcuts help | Present (dashboard scope) | ShortcutsHelp |
| Modal focus traps / `Esc` to close | Present | Dashboard dialogs, ShortcutsHelp |

ccswitch also adds web-specific niceties with no Claude Code analog (`Alt+1–9` jump to session,
`Ctrl+R`/`Ctrl+X` row rename/close, global `↑/↓` list nav off-textarea). Those are out of scope here.

## Gap list (prioritized)

### P1 — Core agent-loop interactions a keyboard user hits constantly

**P1-a. Rapid keyboard answering of permission & question cards.**
`PermissionCard.tsx` and `QuestionCard.tsx` render their options as `<button>`s with **no key
handlers** (`QuestionCard` even uses `role="radiogroup"` but doesn't implement roving-tabindex/arrow
selection). They are operable — you can `Tab` to a button and press `Enter`/`Space` — and the
permission card focuses its primary action (`primaryRef`). But there's no Claude-Code-style fast path:
arrow keys to move between options, number keys (`1`/`2`/`3`) to pick, or `y`/`n` to allow/deny.
Because these cards gate the whole agent loop, this is the highest-value gap.
*Recommend:* add a keyboard layer to both cards — `↑/↓` (or `←/→`) to move a highlighted option,
`Enter` to choose, digit keys `1..9` to jump to an option, and for the permission card `a`=allow /
`d`=deny with the primary already focused so a bare `Enter` approves. Implement once in a shared
`useCardOptionKeys` hook so both cards stay consistent.

**P1-b. Keyboard interrupt of a running turn.**
Interrupt is button-only (`onInterrupt` → the "Stop" button). There is no key that stops an
in-flight turn. In Claude Code `Esc` interrupts generation. In ccswitch, in-session `Esc` currently
**detaches to the list** (`SessionView.tsx:85-90`), so `Esc` is taken.
*Recommend:* make `Esc` status-aware — when `status` is busy/working, `Esc` interrupts the current
turn (and a second `Esc` then detaches); when idle, `Esc` detaches as today. Document the two-stage
behavior in `ShortcutsHelp`. **Conflict to resolve first — see Risks.**

### P2 — High-value, larger or lower-frequency

**P2-a. Double-`Esc` to rewind / edit a previous message.**
A signature Claude Code affordance (tap `Esc` twice to jump back and edit an earlier prompt). No
equivalent exists. This interacts with P1-b's `Esc` overloading, so design them together.
*Recommend:* scope as its own feature — it needs transcript "rewind" semantics (truncate/replace a
prior user turn and resubmit), which is more than a keybinding.

**P2-b. `@` file/path mention autocomplete.**
Claude Code's `@` opens a file picker. ccswitch has `/` commands but no `@` mention. Useful for
referencing repo paths in a prompt.
*Recommend:* reuse the CommandPalette pattern keyed on `@`, backed by a repo file-list endpoint.
Medium effort (needs a backend file-listing source).

**P2-c. Transcript scrolling & tool-output expand/collapse from the keyboard.**
No explicit `PageUp`/`PageDown`/`Home`/`End` handling on the transcript; scrolling relies on the
browser scrolling whatever is focused. Claude Code also has `Ctrl+R` to expand verbose tool output —
ccswitch has no in-session expand/collapse-all key (`Ctrl+R` is repurposed for row-rename on the
dashboard, which is a different scope so no collision).
*Recommend:* make the transcript region focusable and bind `PageUp/PageDown/Home/End`; optionally a
key to toggle expanded tool output. Low-to-medium.

### P3 — Nice to have

- **In-session `?` help overlay.** `?` opens shortcuts only on the dashboard; in-session there's no
  help key. *Recommend:* surface the same overlay (or an in-session variant) from the session view.
- **Vim editing mode** (`/vim`). Absent. Low priority for a mobile-first UI; defer.
- **`Tab` to toggle extended thinking.** Model-level concept; map only if the SDK surfaces it. Defer.

## N/A — terminal-only or agent-handled (documented so they aren't mistaken for gaps)

| Claude Code | Why not applicable to the web layer |
|---|---|
| `Ctrl+C` cancel/exit | Browser reserves `Ctrl+C` for copy; cannot rebind. Use P1-b's `Esc` interrupt instead. |
| `Ctrl+D` exit | No process to EOF in a browser tab. |
| `Ctrl+L` clear screen | The transcript *is* the conversation; closest analog is a `/clear` command, not a screen wipe. |
| `!` bash mode, `#` memory | These are input-prefix modes of the underlying `claude` agent. Whether they pass through depends on how the composer forwards raw text to the session — verify, but they are not web-layer keybindings. |
| Emacs line editing (`Ctrl+A/E/K/U/W`) | Native `<textarea>`/browser editing already covers caret/line editing; OS-dependent. |

## Risks / conflicts to resolve before implementing

1. **`Esc` overloading (P1-b, P2-a, existing detach).** `Esc` currently detaches; making it also
   interrupt (P1-b) and double-`Esc` rewind (P2-a) needs one coherent state machine:
   busy → `Esc` interrupts; idle → `Esc` detaches; `Esc Esc` → rewind. Decide the precedence and the
   guards (composer-with-text already blocks detach) as a single design, not three separate patches.
2. **Card keyboard layer vs. global list nav.** The permission/question cards appear inside the
   session, not the dashboard, so `useGlobalListNav` (dashboard-only) won't collide — but verify the
   new card arrow handling calls `e.preventDefault()`/`stopPropagation()` so it doesn't bubble to the
   SessionView `Esc`/`ArrowLeft` detach handlers.
3. **Mobile.** The app is mobile-first; every keyboard add must keep its touch path (the cards are
   already buttons, which is good — keyboard is additive, not a replacement).

## Suggested sequencing

1. **P1-a** (card keyboard answering) — self-contained, high value, no `Esc` entanglement. Best first.
2. **P1-b** (Esc-to-interrupt) together with the **Risk #1** Esc state-machine design.
3. **P2** items as independent follow-ups, each its own spec → plan → implementation cycle.

Each item above is sized to become its own brainstorming/spec cycle; this report is the backlog, not
a plan.
