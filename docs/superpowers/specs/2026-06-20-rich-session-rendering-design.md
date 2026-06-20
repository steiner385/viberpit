# Rich Session Rendering — Design

**Date:** 2026-06-20
**Status:** Approved (brainstorm) → pending implementation plan
**Author:** ccswitch

## Problem

The session view streams raw PTY bytes (`PTY → base64 → xterm.js`). It is **laggy and hard to read** — it re-renders a TUI that was never meant to be re-rendered. We want the **rich, structured rendering that Claude Code web (claude.ai/code) provides**: message bubbles, collapsible thinking, tool cards, diffs, clickable question buttons, and inline tool-permission approval.

## Key enabling facts (verified against reality)

- Claude writes a **structured JSONL transcript** per session at `<config-dir>/projects/<encoded-cwd>/<session-id>.jsonl`. Event types include `user`, `assistant`, `system`; content blocks include `thinking`, `text`, `tool_use`, `tool_result`, `image`; plus `timestamp`, `uuid`/`parentUuid` (threading), `cwd`, `gitBranch`, and `ai-title`/`customTitle`.
- The `claude` CLI supports structured bidirectional streaming: `--input-format stream-json --output-format stream-json --print --include-partial-messages`, plus `--permission-mode`, `--resume`, `--replay-user-messages`.
- The binary implements the full control protocol: `control_request`/`control_response`, `can_use_tool` (permission round-trip), `interrupt`, `permission_denial`, `tool_permission`, `ai-title`/`customTitle`.
- The Python **`claude-agent-sdk` (0.2.105, PyPI)** wraps this protocol and runs the `claude` binary under the hood, honoring `CLAUDE_CONFIG_DIR` (so it uses the per-account Max OAuth, same as today).
- `projects/` is **symlinked into every account's config dir** (`configdir.SHARE_ALLOWLIST`), so a session's transcript is visible to all accounts — which is what makes `--resume` work across accounts on rotation.

## Decisions (from brainstorm)

1. **Interaction model:** full rebuild — rich transcript + prompt box + click-to-approve, dropping the raw terminal. *(Not: a viewer over the existing PTY, nor a hybrid with a terminal escape hatch.)*
2. **Tool approvals:** configurable — a **default setting** (interactive / auto-approve / allowlist) **and** a **per-session toggle**.
3. **v1 must-haves:** interrupt/stop a running turn; slash commands; plan mode; queue input while busy; **render prompt questions (AskUserQuestion) as clickable buttons**; **auto-set session name** from conversation content.
4. **Driver mechanism:** **Approach A — the Python Claude Agent SDK**, with raw stream-json (Approach B) as the documented fallback if the resume-on-rotation spike fails.

## Architecture

### Backend — `SessionDriver` replaces the PTY for sessions

One `SessionDriver` per session, wrapping the SDK's `ClaudeSDKClient`. Started with:
- `cwd` = the session's worktree (existing `.ccswitch/worktrees/...` logic),
- `CLAUDE_CONFIG_DIR` = the composed account dir (OAuth),
- a `can_use_tool` callback, an initial `permission_mode`,
- `resume=<claude_session_id>` when rotating/resuming.

It exposes: `send_prompt(text)`, `interrupt()`, `set_permission_mode(mode)`, `resolve_permission(permId, decision)`, `answer_question(questionId, selected)`, and an **async out-stream of normalized events**.

A **translator** maps SDK messages → ccswitch's normalized events. Nothing downstream sees the SDK wire format.

`SessionOrchestrator` keeps its current responsibilities — account selection, rotation, lifecycle, DB persistence — but manages **drivers** instead of PTYs. `manager.py`/PTY code is retired for sessions.

The WS bridge stops sending base64 terminal frames and relays normalized event JSON.

### The normalized event contract (the decoupling seam)

**server → client**

| event | purpose |
|---|---|
| `turnStart {turnId}` / `turnEnd {turnId, usage}` | turn boundaries |
| `textDelta {turnId, text}` / `thinkingDelta {turnId, text}` | streamed assistant text & collapsible thinking |
| `toolCall {turnId, toolId, name, input}` | a tool card appears |
| `toolResult {toolId, ok, payload}` | structured result (diff for Edit/Write, content for Read, stdout/stderr+exit for Bash, todo list, JSON fallback) |
| `permissionRequest {permId, tool, input, kind}` | Yes/No/Always card; `kind="plan"` → Approve & run / Keep planning |
| `question {questionId, header, question, options[], multiSelect}` | AskUserQuestion → clickable buttons |
| `title {title, source}` | auto session name |
| `status {state}` | idle / working / waiting-permission / waiting-input / queued(n) |
| `rotated {from,to}` · `error {code,message}` · `replayComplete` | lifecycle |

**client → server**

| event | purpose |
|---|---|
| `prompt {text}` | send a prompt (slash commands pass through verbatim) |
| `interrupt` | stop the running turn |
| `permissionResponse {permId, decision: allow\|deny\|always}` | approval |
| `answerQuestion {questionId, selected[]}` | clicked option(s) |
| `setPermissionMode {mode}` | plan-mode / approval-mode switch |

### History on attach

On attach, the backend replays prior turns by reading the session's **JSONL transcript** → normalized events → client, then emits `replayComplete`, then switches to live driver events. Reconnects and reloads restore full rich history without holding it all in memory.

## Turn lifecycle & flows

**Normal turn:** `prompt` → `turnStart` → (`textDelta`|`thinkingDelta`)\* → `toolCall`/`toolResult`\* → `turnEnd`; status idle. Deltas stream via `--include-partial-messages`.

**Permission round-trip:** SDK fires `can_use_tool(tool,input)` → driver creates `permId`, emits `permissionRequest`, **parks on an `asyncio.Future`**; client `permissionResponse` resolves it → allow/deny returned to the SDK. `always` adds the tool to a **per-session allowlist** (future calls auto-resolve, no event). Auto-approve mode returns allow immediately; allowlist mode auto-allows read-only tools and round-trips mutating ones.

**Interrupt:** `interrupt` → `driver.interrupt()` → `turnEnd`; any pending permission Future auto-denies.

**Queue while busy:** a `prompt` arriving while status≠idle is enqueued (status `queued:n`, muted pending bubbles, cancelable); dequeued on `turnEnd`.

**Plan mode:** `setPermissionMode {plan}`; the assistant proposes a plan then calls `ExitPlanMode`, surfaced as a `permissionRequest{kind:"plan"}` → Approve sets mode back to the session default and execution continues.

**Question buttons:** model calls AskUserQuestion → `question` event → client `answerQuestion` → driver returns it as the tool result → model continues. *(Spike: confirm AskUserQuestion is offered in SDK headless mode and fulfilled via the tool/permission path. Fallback: not used; the model asks in plain text.)*

**Slash commands:** typed in the Composer, sent verbatim. Text commands (`/clear`, `/compact`, custom) work over stream-json; interactive pickers (`/resume` chooser) are unneeded — ccswitch has its own session list.

**Auto-title:** driver emits `title` from the transcript's `ai-title`/`customTitle`; fallback = first prompt truncated. Persisted to the session row, replacing "New Session."

**Rotation mid-turn:** on ceiling hit: `driver.interrupt()` → pick account Y → new `SessionDriver(resume=<claude_session_id>, cfg=Y)` → re-send the interrupted prompt → `rotated{X→Y}`; UI shows an inline divider and the turn restarts on Y. Works because `projects/` (the transcript) is shared across account config dirs.

## Frontend

`SessionView` replaces `Terminal.tsx`. It subscribes to normalized events for its `sessionId` and runs them through a **reducer that builds a transcript model** (`turns[] → blocks[]`); `textDelta`/`thinkingDelta` append into the open block (event-sourced client state).

```
SessionView
├─ TranscriptView            (scrollable; sticky auto-scroll, "jump to latest")
│  └─ Turn
│     ├─ UserBubble
│     ├─ AssistantText        markdown + syntax-highlighted code
│     ├─ ThinkingBlock        collapsible, collapsed by default
│     ├─ ToolCallCard         icon + name + key arg; expandable input
│     │   └─ ToolResult →     DiffView · FileContent · BashResult · TodoList · JSON fallback
│     ├─ PermissionCard        [Yes] [No] [Yes, always]  | plan: [Approve & run] [Keep planning]
│     ├─ QuestionCard          header + option buttons (single/multi-select)
│     └─ RotationDivider       "↻ switched kindash → secondary"
├─ Composer                   textarea + Send · Stop (working) · Normal/Plan toggle · queued strip
└─ StatusBar                  idle / working / waiting-permission / queued(n)
```

**Reuse & libs:** keep `SessionSocket` (extend its event union); carry over the aria-live mirror (announce new assistant text + permission/question prompts). Add `react-markdown` + `remark-gfm`, a lazy-loaded syntax highlighter (Shiki), and a diff renderer. Heavy renderers are code-split.

**Mobile-first:** sticky bottom Composer, full-width cards, tool inputs collapsed by default (tap to expand).

## Settings

- `GET/PUT /api/settings` backed by a single settings row: **default permission mode** (interactive/auto/allowlist), default "thinking collapsed", default repo-clone root.
- New Session dialog gains a "Tool approvals" select, pre-filled from the default.
- Live in-session control: Composer's Normal/Plan toggle = live `permission_mode`; a small menu flips interactive/auto/allowlist mid-session (plus the per-session "Yes, always" allowlist).

## Error handling & edge cases

- **Driver death:** emit `error`+`status`; offer auto-restart with `resume` (transcript intact); repeated failures → `errored`.
- **No hung tools:** disconnect/interrupt/close resolves every pending permission/question Future as deny.
- **Reconnect:** replay from JSONL → `replayComplete` → live; the driver tracks open permission/question requests and **re-emits them on reattach**.
- **Rotation:** all accounts limited → `error` + wait/retry; a rotation with an open card carries the pending request over.
- **needs-reauth:** can't start driver → `error` suggesting re-add.
- **Large outputs:** truncate in-card with "show more"; cap in-memory transcript; full history recoverable from JSONL.
- **Backpressure:** coalesce deltas on ~16ms flushes (reuse today's batcher).

## Testing

- **Backend (no real claude, no quota):** inject a **fake SDK client** with recorded message fixtures → assert translator output; permission-Future bridge (allow/deny/always, interrupt/disconnect → deny); queue; rotation-mid-turn (mock driver); JSONL→events replay; WS integration (attach → replay → `replayComplete` → prompt → events).
- **Frontend:** reducer tests (events → model); component tests for `DiffView`, `ToolCallCard`, `PermissionCard`, `QuestionCard`, `Composer` states (Vitest + RTL).
- One **opt-in live smoke** with a tiny real session (manual, not CI). All existing suites stay green (core unittest, server pytest, web vitest, typecheck, build).

## Migration (clean cutover — "purge old/unused" rule)

- **Delete in the same change:** `Terminal.tsx`, `HelperKeyBar`, `@xterm/*` deps, base64 output/input plumbing in `ws.py` + `client.ts`, the PTY `SessionManager`.
- Single-user tool → clean branch→PR cutover (no feature flag). Optional dev-only "raw events" debug panel (not xterm).
- **Data model:** add `title` + `permission_mode` to the session row; add a `settings` row.
- Driver swap A→B later is invisible to the UI (the event contract is the seam).

## Open feasibility spikes (resolve early in the plan)

1. **Resume-on-rotation under the SDK** — confirm `ClaudeSDKClient` resumes a session id across config dirs (different account) using the shared `projects/` transcript. If not, fall back to Approach B (raw stream-json).
2. **AskUserQuestion in SDK headless mode** — confirm it is offered and fulfillable via the tool/permission path; otherwise it degrades to plain-text questions.
3. **OAuth via SDK** — confirm the SDK uses the composed `CLAUDE_CONFIG_DIR` Max credentials (no API key required).

## Out of scope (nice-to-have follow-ups)

- Interactive slash-command pickers; image paste/attach into prompts; multi-pane/split views; transcript search; exporting a session.
