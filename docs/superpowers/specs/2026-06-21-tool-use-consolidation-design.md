# Tool-Use Consolidation in Agent View — Design

**Date:** 2026-06-21
**Feature:** 001-ccswitch-web (Agent / structured session view)
**Status:** Proposed

## Problem

The structured Agent View renders every Claude tool use as a full bordered
`ToolCallCard`. Each card carries its own header row plus a separate result
toggle row. A turn that runs many tools (a typical agentic loop reads several
files, greps, edits, runs bash) becomes a tall stack of heavy boxes. Even with
args/results collapsed by default, the sheer number of bordered cards is noisy
and makes it hard to follow the conversation — the same problem Claude Code
solves by folding completed tool calls into compact one-line entries and
collapsing long runs.

The user's request: "the ability to consolidate/collapse the individual tool
uses like Claude Code does. This is too noisy."

## Goals

- Drastically reduce vertical noise from tool-use rendering in the Agent View.
- Keep the most relevant detail visible: the in-flight / most-recent tool stays
  open; finished tools fold away.
- Let the user expand any tool, any group, or everything on demand.
- Pure presentation change — no backend, websocket, or protocol changes.

## Non-Goals (v1)

- Persisting collapse state across reloads (localStorage). Ephemeral only.
- Collapsing non-tool blocks (`thinking` already has its own block component;
  `text` is the conversation itself).
- Any change to the raw xterm.js terminal view — it renders an opaque ANSI
  stream and cannot be structurally folded.

## Background: current architecture

- The reducer (`web/src/session/reducer.ts`) turns websocket events into
  `Turn[]`. Each `Turn` has `role` and an ordered `blocks: Block[]`.
- `Block` is a tagged union: `text | thinking | tool | permission | question`.
- A `tool` block: `{ kind:'tool'; id; toolId; name; toolKind; input; result? }`.
  **There is no explicit running flag** — a tool is *complete* iff
  `result !== undefined`.
- `Turn.tsx` maps each block to a component; `tool` → `ToolCallCard`.
- `ToolCallCard.tsx` already supports per-card collapse of its *args* panel and
  *result* panel, and accepts `defaultResultOpen` for the most recent tool.

The key enabler: tool uses are first-class structured objects, so consolidation
is purely a render/altitude concern.

## Design

Three coordinated changes.

### A. Compact-by-default tool rows (`ToolCallCard` density)

Add a `density` prop to `ToolCallCard`:

```ts
interface Props {
  block: ToolBlock
  density?: 'line' | 'card'   // default 'line'
  defaultResultOpen?: boolean
}
```

- `'line'` (new default): render a single clickable row with no surrounding
  bordered box — `icon · name · key-arg · ✓/✕ status · size-hint · chevron`.
  Clicking the row toggles an inline detail panel (result, plus an args
  sub-toggle). This reuses the existing `keyArg` / `sizeHint` / `kindIcon`
  helpers and the existing `ToolResult` panel.
- `'card'`: the current full bordered layout, preserved unchanged for any
  caller that wants it and to minimize churn/regression risk.

The most-recent tool in the live turn passes `defaultResultOpen` so its output
is visible without a click (preserves today's behavior).

### B. Group fold for consecutive tool runs (`ToolGroup`)

A pure helper coalesces a turn's blocks into render items:

```ts
type RenderItem =
  | { kind: 'block'; block: Block }
  | { kind: 'toolGroup'; tools: ToolBlock[] }

function groupBlocks(blocks: Block[]): RenderItem[]
```

`groupBlocks` walks `blocks` and merges maximal runs of consecutive
`kind:'tool'` blocks into one `toolGroup`; all other blocks pass through as
`block`. Pure and order-preserving → unit-tested like `reducer.test.ts`.

New `ToolGroup` component decides how to render a run:

- **Collapsed summary** when the group is *complete* (every tool has a
  `result`) AND it is *not the live tail* AND `tools.length >= GROUP_THRESHOLD`
  (default 4): render one line — `✓ 6 tool calls — Read, Edit, Bash… (expand)`
  (names deduped, truncated). Clicking expands to the compact rows.
- **Expanded rows** otherwise (live tail, short group, or any incomplete tool):
  render each tool as a `density='line'` `ToolCallCard`.

"Live tail" = the group is the last render item of the last assistant turn (it
contains the newest tool activity). `SessionView`/`Turn` already know which turn
is last; the live-tail flag is passed down as a prop.

### C. Global expand/collapse-all toggle

A single control to flip all tool detail at once:

- A header/StatusBar button (collapse-all / expand-all icon).
- A keybinding wired into the existing `SessionView` `window` keydown handler
  (same pattern as Esc/Shift+Tab), guarded so it doesn't fire while typing in
  the Composer. Candidate: a single unmodified key when the transcript is
  focused, or a documented chord — final binding chosen during implementation
  to avoid collisions, then added to `ShortcutsHelp`.

Mechanism: a `toolDetailMode: 'auto' | 'all-open' | 'all-closed'` value owned by
`SessionView`, passed to `Turn` → `ToolGroup` → `ToolCallCard`. `'auto'`
applies the per-group/per-card defaults above; the other two force-override.

## Data flow

```
reducer (unchanged) → SessionView (owns toolDetailMode, knows last turn)
  → Turn (calls groupBlocks, marks live tail)
    → ToolGroup (fold decision)
      → ToolCallCard density='line' (per-tool inline expand)
```

No reducer, event, or server changes. `groupBlocks` is the only new pure unit;
everything else is component state (`useState`) that is ephemeral by design.

## Error / edge cases

- **Incomplete tool in an otherwise-finished group:** group stays expanded
  (never hide an in-flight tool).
- **Single tool / short run (< threshold):** never shows a fold header; renders
  as a compact row directly.
- **Mixed blocks** (tool, then text, then tool): two separate groups; text
  passes through between them. Order is always preserved.
- **Empty result payloads:** `sizeHint` already returns `''` gracefully; the
  row simply omits the hint.
- **`toolDetailMode='all-closed'` with an in-flight tool:** force-collapse is a
  user-explicit action and may hide a running tool's output; acceptable since
  the status row still shows running state and the user can expand. (Revisit if
  it proves confusing.)

## Testing

- **Unit (`groupBlocks`):** consecutive tools coalesce; non-tool blocks split
  groups; order preserved; empty input; single block. Mirrors
  `web/src/session/reducer.test.ts`.
- **Component (`ToolGroup`):** complete + ≥threshold + not-live → renders fold
  summary; clicking expands rows; live tail renders expanded; incomplete tool
  forces expanded.
- **Component (`ToolCallCard` line mode):** row toggles inline detail; args
  sub-toggle works; `defaultResultOpen` opens result; `card` mode unchanged.
- **Accessibility:** fold/expand controls keep `aria-expanded` / `aria-controls`
  (existing pattern) and 44px min touch targets (mobile-first per project).

## Defaults summary

| Setting | Default |
|---|---|
| `ToolCallCard` density | `'line'` |
| Group fold threshold | 4 tools |
| Live tail | always expanded |
| Most-recent tool result | auto-open |
| Collapse-state persistence | none (ephemeral) |
| `toolDetailMode` | `'auto'` |

## Rationale / alternatives considered

- **Per-card collapse only (status quo):** already exists; doesn't solve the
  "too many boxes" problem. Rejected as insufficient.
- **Collapse everything to one line per turn always:** hides too much; the live
  tool's output is what the user usually wants. Rejected.
- **Persist collapse state in localStorage:** real nice-to-have, but adds
  storage keys + migration surface for marginal v1 value. Deferred (YAGNI).

Chosen approach mirrors Claude Code's model — compact finished tools, keep the
live one open, fold long runs, allow expand-all — at the lowest implementation
risk because it touches only the render layer plus one pure helper.
