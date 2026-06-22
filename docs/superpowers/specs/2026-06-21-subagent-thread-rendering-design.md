# Sub-Agent Thread Rendering — Design

**Date:** 2026-06-21
**Feature branch:** `002-subagent-thread-rendering`
**Status:** Approved design, pending implementation plan

## Problem

The sessions UI flattens sub-agent activity into the main conversation thread. A
sub-agent's own thinking, text, and tool calls appear interleaved with the parent
agent's output instead of being shown as a separate, attributable thread. With
parallel sub-agents (multiple `Task` calls in one turn), several agents' streams
collide in a single turn container.

### Root cause (verified)

The `claude-agent-sdk` tags **every** streamed message with `parent_tool_use_id`:

- `None` → the message belongs to the **main thread**.
- a `Task` tool-use id → the message was emitted by the **sub-agent** that tool spawned.

This field is present on `AssistantMessage`, `UserMessage`, and `StreamEvent`
(`claude_agent_sdk/types.py`), and the SDK's `_internal/message_parser.py`
populates it from the wire JSON.

`server/sessions/translator.py` **ignores it entirely** (a grep for
`parent_tool_use_id` across `server/` returns zero hits). Every event the
translator emits is stamped with `self._turn_id` — the *main* turn — regardless of
which agent produced it. So sub-agent events are mis-attributed into the parent's
assistant turn.

The frontend already encodes the (incorrect) assumption behind this: the comment in
`web/src/session/selectors.ts` reads *"the parent never sees their internal steps,
only the spawn and the result."* The SDK does surface those steps; they are simply
mis-attributed. `selectSubAgents()` only detects the `Task` tool spawn and its
result, surfaced as a status-bar popover.

Claude Code itself uses this same `parent_tool_use_id` linkage and renders each
`Task` as a collapsible card whose nested content **is** that sub-agent's own
thread. That is the behavior to replicate.

## Goal

Render each sub-agent as a nested, collapsible thread attached to the `Task` card
that spawned it — main thread stays clean; expanding a `Task` card reveals that
sub-agent's own mini-transcript. Match Claude Code's convention.

## Core mechanism — the linkage key

A sub-agent's `parent_tool_use_id` equals the id of the `Task` tool-use that spawned
it. That id is **already** the `toolId` of the `Task` block in the main transcript.
So the linkage is free: a sub-agent's events attach to the main-thread `Task` card
whose `toolId` matches `parentToolUseId`. No separate correlation scheme is needed.

## Design

### Backend — `server/sessions/`

- **`events.py`** — add `parent_tool_use_id: str | None` (wire field
  `parentToolUseId`) to the events that can originate from a sub-agent: `TurnStart`,
  `TurnEnd`, `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`. Default `None`
  so existing call sites and replay remain valid.

- **`translator.py`** — read `getattr(msg, "parent_tool_use_id", None)` on
  `AssistantMessage` / `UserMessage` / `StreamEvent` and stamp it onto every emitted
  event. Replace the single `_turn_id` / `_started` scalar state with **per-agent
  turn state keyed by `parent_tool_use_id`** (`None` = main thread). A sub-agent's
  `message_start` opens its own turn rather than collapsing into the parent's. The
  global `_seen_tools` set is retained (tool ids are globally unique).
  `ResultMessage` still ends the main turn; a sub-agent card closes when its `Task`
  `ToolResult` arrives on the main thread (existing status logic, unchanged).

### Frontend — `web/src/`

- **`session/events.ts`** — add `parentToolUseId?: string | null` to the matching
  `Ev*` interfaces (`EvTurnStart`, `EvTurnEnd`, `EvTextDelta`, `EvThinkingDelta`,
  `EvToolCall`, `EvToolResult`).

- **`session/reducer.ts`** — add
  `subAgents: Record<string /* toolId */, { turns: Turn[]; openBlockId: string | null }>`
  to `TranscriptState`. Events carrying `parentToolUseId` route into that sub-state;
  events without it remain in the main `turns`.

  This sub-state also fixes a latent bug: the reducer tracks a single global
  `openBlockId` (the currently-streaming text/thinking block). When a sub-agent and
  the parent stream text concurrently into one turn, that single pointer thrashes and
  text fragments interleave. Giving each agent thread its own `openBlockId` is what
  makes concurrent streaming render correctly — the data-structure choice and the
  visual separation are the same fix.

- **`components/session/ToolCallCard.tsx`** (or a new `SubAgentCard`) — for
  `name === 'Task'`, render a collapsible card:
  - **Collapsed (default):** `subagent_type` badge + description + running/done/failed
    status + final result (today's view).
  - **Expanded:** the sub-agent's mini-transcript, reusing the existing `Turn` /
    `Block` components against `state.subAgents[toolId]`.

  `SubAgentList` popover is retained as a jump-to index.

### Scope boundary — live vs. replay

- **This change (live):** fix live rendering + main-thread cleanliness. Additionally,
  make `replay.py` **skip** any entry with `isSidechain === true`, so older
  single-file (Layout A) transcripts cannot leak sub-agent steps into the main thread
  on reattach.

- **Follow-up (explicitly out of scope here):** reconstruct full sub-agent threads on
  replay by reading the `subagents/agent-*.jsonl` files and `.meta.json` sidecars
  (Layout B). Until then, on reattach a sub-agent shows its spawn + final result but
  not its internal steps.

## Testing (TDD, per constitution Principle V)

- **Backend (`server/tests/`):** translator tests feeding fake SDK messages with
  `parent_tool_use_id` set — main-only, a single sub-agent, and two parallel
  sub-agents — asserting (a) every emitted event carries the correct
  `parent_tool_use_id`, and (b) sub-agent turns are separated from the main turn.
  A replay test asserting `isSidechain` entries are skipped.

- **Frontend (`web/`):** reducer tests asserting tagged events route into
  `subAgents[toolId]` (and untagged events stay in `turns`), including concurrent
  text streaming into parent + sub-agent without fragment interleaving. A render test
  for the collapsible `Task` card (collapsed summary, expanded mini-transcript).

## Constraints / governance

- Single account per session and its sub-work (constitution II; spec out-of-scope
  note: sub-agents are not split across accounts). This change is presentation-only
  on the existing event stream — no change to account isolation.
- New SQLAlchemy columns are not required; this is wire/UI state only.
