# Sub-Agent Session Drill-Down — Design

**Date:** 2026-06-22
**Feature:** 001-ccswitch-web
**Status:** Proposed

## Problem

When a `claude` session uses the `Task` tool to spawn a sub-agent, the sub-agent's
own responses, thinking, and tool-uses currently **leak into the parent's main
conversation thread**. They appear as if the main agent produced them, with no
separation. This is wrong on two independent paths:

- **Live path** (`server/sessions/translator.py`): the SDK delivers every message with
  a `parent_tool_use_id` field. When it's non-null, that message came from a sub-agent
  spawned by the `Task` tool of that id. The `Translator` ignores the field and folds
  the sub-agent's text/tools straight into the active main turn.
- **Replay path** (`server/sessions/replay.py`): Claude's JSONL transcript marks
  sub-agent lines with `isSidechain: true` / `parentUuid`. `replay.py` doesn't filter
  them, so on re-attach and archived review they replay as ordinary main-thread turns.

Today the only sub-agent affordance is `SubAgentList`, which sniffs `Task` tool calls
for a status dot — it never had the sub-agent's actual content.

## Goals

1. **Main thread shows only main-agent content** (`parent_tool_use_id == null`). The
   `Task` tool-call card stays as the visible anchor; its sub-agent body does not appear inline.
2. **Sub-agents are reviewable as hierarchical sub-sessions** nested under their parent
   session in the session list, to **arbitrary depth** (a sub-agent that spawns a
   sub-agent nests recursively).
3. **Clicking a sub-agent row opens its transcript in the main pane**, read-only, with a
   breadcrumb (`Parent › Sub-agent`) to navigate back up. Reuses the existing `ReviewView`.
4. **Live**: a sub-agent row appears the moment its `Task` fires, shows a `running`
   status, and its transcript streams live as it works.

## Non-Goals

- No new OS processes. Sub-agents are **not** separate `claude` PTYs — they run inside the
  parent process. A sub-session is a **derived, read-only projection** of the parent's
  event stream, not a managed session. (Keeps Constitution Principle II intact.)
- No split/side-by-side panes (deferred). Sub-sessions open in the main pane.
- No editing/sending into a sub-agent (it has no independent input channel).

## Architecture Overview

The whole feature is an **attribution + grouping layer** over events we already receive.
One new piece of identity threads through everything: **`agent_id`** — the
`parent_tool_use_id` (the spawning `Task` tool's id) that owns an event. `agent_id == null`
means the main agent. Deeper nesting works because each `Task` tool-use itself has a
`tool_id`, and a grandchild's events carry the grandchild's `parent_tool_use_id`, while
the child `Task` card lives in *its* parent's bucket — chaining the tree.

```
SDK / JSONL  ──►  Translator / Replay   ──►  events tagged agent_id  ──►  WS
                  (per-agent turn state)                                    │
                                                                            ▼
                                              Reducer: bucket events by agent_id
                                              ├─ buckets[null]      → main TranscriptView
                                              ├─ buckets[toolu_A]   → sub-session A
                                              └─ tree of sub-agents → SessionList nesting
```

## Backend

### 1. Event model (`server/sessions/events.py`)

Add an optional `agent_id: str | None = None` field to the content-bearing events
(`TurnStart`, `TurnEnd`, `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`,
`UserMessage`). Each `to_wire()` emits `"agentId": self.agent_id`. Default `None` keeps
every existing producer and the wire contract backward-compatible (absent/null = main).

New event **`SubAgentUpdate`** — a lightweight registry push so the session list can show
the tree without parsing a transcript:

```python
@dataclass
class SubAgentUpdate:
    agent_id: str               # the Task tool_use id
    parent_agent_id: str | None # owning agent (null = main); enables the tree
    agent_type: str             # subagent_type from Task input (e.g. "Explore")
    description: str            # description from Task input
    status: str                 # "running" | "done" | "failed"
    def to_wire(self): ...      # {"type": "subAgentUpdate", ...}
```

### 2. Live translation (`server/sessions/translator.py`)

The `Translator` currently holds **one** turn context (`_started`, `_turn_id`,
`_seen_tools`). Sub-agent messages interleave with main, so this must become
**per-agent-context state keyed by `parent_tool_use_id`** (`None` for main):

- Replace the scalar fields with dicts keyed by `agent_id`.
- `feed()` reads `getattr(msg, "parent_tool_use_id", None)` for every message type
  (`StreamEvent`, `AssistantMessage`, `UserMessage`, `ResultMessage`) and routes
  turn-open / delta / tool / turn-end into that context.
- Every emitted event is tagged with `agent_id`.
- When a main-agent `ToolCall` with `name == "Task"` is emitted, also emit a
  `SubAgentUpdate(agent_id=tool_id, parent_agent_id=<current ctx>, type/description from
  input, status="running")`. On the matching `ToolResult`, emit a follow-up
  `SubAgentUpdate(status="done"/"failed")`.

This is the most delicate change (interleaved streaming state) and gets the most unit
coverage (see Testing).

### 3. Replay (`server/sessions/replay.py`)

- Tag each emitted event with `agent_id`. For an entry, `agent_id` is derived from
  `isSidechain` + the `parentUuid` chain back to the `Task` tool-use that spawned the
  sidechain root.
- Emit `SubAgentUpdate` entries from the `Task` tool-uses found in main-agent turns, with
  terminal status inferred from whether the matching `tool_result` is present/errored.

> **Verification gate (Constitution III — verify against the real binary):** the exact
> JSONL field that links a sidechain entry to its spawning `Task` `tool_use_id` must be
> confirmed against a **real captured transcript that contains a sub-agent**, not assumed.
> The implementation plan's first step is to capture such a transcript, add it as a test
> fixture (`server/tests/fixtures/transcript_subagent.jsonl`), and pin the linkage logic
> to it. If the JSONL does not directly carry the `Task` id, we reconstruct grouping from
> distinct sidechain `parentUuid` roots and associate each to a `Task` tool-use in
> transcript order. This is the one piece we explicitly do not design from memory.

### 4. Sub-agent registry (`server/sessions/orchestrator.py`)

For **active** sessions, the orchestrator keeps an in-memory per-session
`dict[agent_id → SubAgentSummary]`, updated as `SubAgentUpdate` events flow. This backs
two needs cheaply (no transcript re-parse):

- The sessions-list payload includes a compact sub-agent tree per session.
- Re-attach replays the registry alongside transcript events.

For **archived** sessions, the tree is derived from the JSONL on demand. To avoid scanning
every archived transcript on each list call, persist a compact summary.

### 5. Persistence (`server/db/models.py`)

Add a **nullable** `sub_agents` JSON column to `Session` (per CLAUDE.md: new columns must
be nullable so `init_db` backfill works without a migration). Written/updated as a session
runs and at archive time; holds the compact tree
(`[{agentId, parentAgentId, type, description, status}]`). Null/empty = no sub-agents.

### 6. API (`server/api/sessions.py`)

- **Session list** response: each session gains `subAgents: SubAgentSummary[]` (the tree;
  empty when none). Active → from orchestrator memory; archived → from the persisted column.
- **Transcript** endpoint (`GET /{id}/transcript`): unchanged shape, but events now carry
  `agentId` and the stream includes `subAgentUpdate` events. A sub-session view requests
  the parent's transcript and filters client-side by `agentId` (no new endpoint needed;
  one fetch already holds the whole tree's data). Redaction path unchanged.

## Frontend

### 1. Events & reducer (`web/src/session/events.ts`, `reducer.ts`)

- Add optional `agentId?: string | null` to the content event types; add an
  `EvSubAgentUpdate` type.
- The reducer changes from producing a flat `Turn[]` to producing:
  - `buckets: Map<string|null, Turn[]>` — events folded into the bucket for their
    `agentId` (main = the `null` key).
  - `subAgents: Map<string, SubAgent>` — folded from `subAgentUpdate` events, each with
    `parentAgentId`, `type`, `description`, `status`.
- `selectSubAgents` (`web/src/session/selectors.ts`) is rewritten to build the **tree**
  from `parentAgentId` links (replacing today's `Task`-call sniffing), and a
  `selectTranscript(agentId)` selector returns the right bucket.

### 2. Main thread (`TranscriptView` / `SessionView`)

`SessionView` feeds `TranscriptView` the `buckets[null]` turns only. The `Task` tool card
(`ToolCallCard`) renders as today — it's the anchor. Net effect: **sub-agent content no
longer appears in the main thread.**

### 3. Session list nesting (`SessionList.tsx`, `SessionCard.tsx`)

`SessionCard` gains an expand/collapse chevron when `session.subAgents` is non-empty.
Expanding reveals nested, indented sub-agent rows rendered **recursively** (a sub-agent
with children is itself expandable), each showing status dot + type + description. A row is
selectable like a session and calls `onOpen(sessionId, agentId)`.

### 4. Sub-session view (`ReviewView.tsx` + breadcrumb)

Opening `(sessionId, agentId)` reuses `ReviewView`: it fetches the parent transcript,
folds it through the reducer, and renders `buckets[agentId]` read-only. A breadcrumb header
(`Parent title › sub-agent type`, with deeper chains for nested agents) navigates back up.
Live sub-sessions subscribe to the same WS stream and render incrementally; status comes
from the `subAgents` map.

## Data Flow

- **Live:** SDK msg → `Translator` routes by `parent_tool_use_id` → tagged events +
  `SubAgentUpdate` → WS → reducer buckets → main thread shows `null` bucket; session list
  grows sub-agent rows live; open sub-session streams its bucket.
- **Re-attach:** orchestrator replays transcript (tagged) + registry → same as above.
- **Archived:** list uses persisted `sub_agents`; opening a sub-session fetches the
  transcript and filters by `agentId`.

## Error Handling & Edge Cases

- **Unknown `parent_tool_use_id`** (sub-agent event arrives before its `Task` card): create
  a placeholder sub-agent entry keyed by the id; backfill type/description when the `Task`
  call is seen. Never drop events.
- **Orphaned sidechain in replay** (no resolvable `Task` link): group under a synthetic
  "(detached sub-agent)" node rather than leaking into main.
- **Deep nesting:** purely data-driven via `parentAgentId`; no depth cap. UI indents but
  caps visual indent to keep rows readable.
- **`AskUserQuestion` inside a sub-agent:** stays attributed to that sub-agent's bucket; it
  is read-only in review (sub-agents can't take new input).
- **Redaction:** unchanged — applied per event regardless of `agentId`.

## Testing

- **`replay.py`**: new fixture with a real sub-agent transcript; assert sidechain events
  get the right `agentId`, main bucket excludes them, `SubAgentUpdate`s emitted with
  correct tree + terminal status. Orphan/nested cases.
- **`translator.py`**: feed interleaved main+sub-agent SDK message sequences (incl. nested);
  assert per-context turn state, correct `agentId` tagging, `SubAgentUpdate` on Task
  fire/result. This is the highest-risk unit — cover interleaving thoroughly.
- **API**: list payload includes `subAgents` tree (active + archived); transcript stream
  carries `agentId`/`subAgentUpdate`.
- **Frontend (vitest)**: reducer buckets by `agentId`; `selectSubAgents` builds correct
  tree; `TranscriptView` main bucket excludes sub-agent content; `SessionList` renders
  nested rows and recursion; `ReviewView` renders a chosen bucket with breadcrumb.
- TDD throughout (Constitution V). `tsc --noEmit`, `eslint`, `vitest run`, `pytest` green.

## Build Order (phases for the plan)

1. **Capture + pin replay fixture** (verification gate) — real sub-agent transcript.
2. **Backend attribution** — `agent_id` on events; `Translator` per-context state; replay
   tagging; `SubAgentUpdate`.
3. **Registry + persistence + API** — orchestrator map, nullable `sub_agents` column, list
   payload, transcript stream.
4. **Frontend reducer/selectors** — buckets + tree; main thread filtered to `null`.
5. **Frontend UI** — `SessionList` nesting, sub-session `ReviewView` + breadcrumb, live
   running status.

## Observed sidechain linkage (verified 2026-06-22)

Verification against real data (1,665 ccswitch SDK transcripts under
`~/.claude-accounts/cfg/*/projects/*/*.jsonl` and interactive Claude Code transcripts under
`~/.claude.backup/projects/*`) found:

- **Live path is authoritative and unchanged in plan:** the SDK delivers `parent_tool_use_id`
  on `AssistantMessage`/`UserMessage`/`StreamEvent`. This is the sub-agent link the
  `Translator` uses. Solid.
- **No existing ccswitch transcript contains a real sub-agent run.** `isSidechain:true`
  appears 0 times; every `parent_tool_use_id` string hit was source code quoted *inside*
  transcript text (always null as an actual field). So the SDK's on-disk sub-agent format
  could not be pinned from existing data.
- **Conversation entries use the Claude Code schema:** top-level `uuid`, `parentUuid`,
  `isSidechain` (normally `false`), `message.content` (string or block list). Interactive
  Claude Code writes sub-agent turns to **separate `agent-<shortid>.jsonl` files** carrying
  an `agentId` field; the SDK *may* instead write them inline with `isSidechain:true`.

**Decision (user-approved):** the replay parser is written **defensively to tolerate both
shapes** without depending on an unverified one:
  1. If an entry has a non-null `parent_tool_use_id` (top-level or in `message`), use it as
     `agent_id` directly (mirrors the live path; simplest, future-proof).
  2. Else if `isSidechain` is truthy, group by `parentUuid` chains → distinct sidechain
     roots, associated to main-agent `Task` tool-uses in document order (fallback B).
  3. Else `agent_id = None` (main agent).
A hand-authored fixture (`transcript_subagent.jsonl`) exercises the `isSidechain`+`parentUuid`
inline shape (the single-file case `replay.py` reads). A real sub-agent transcript will be
captured during the final manual smoke to confirm before merge.
