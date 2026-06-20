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

---

## Security model (added after 8-persona review)

Threat model: single-user, tailnet-only — but the composer + `can_use_tool` is a **privileged remote-code-execution channel** and must be designed as one.

- **Per-message WS authorization.** Identity is re-verified on *every* inbound WS frame (`prompt`, `interrupt`, `permissionResponse`, `setPermissionMode`, `answerQuestion`), not just at handshake; reject on identity change / revoked AppSession. Each `permId`/`questionId` is bound to the session creator's identity; responses from any other identity are dropped. Responses for unknown/already-resolved ids are silently dropped.
- **Permission defaults & scoping.**
  - **Ships as `interactive`.** `auto`/`allowlist` are opt-in. By default `auto` applies a hardcoded **dangerous-tool denylist** (`Bash`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `WebFetch`, `WebSearch`, un-whitelisted MCP tools) that still round-trips.
  - **"Full auto (incl. shell)"** removes the denylist. Per the owner's decision it MAY be selected as the *global* default (one-click hands-off across all sessions), not only per-session — with a persistent banner and the explicit, documented acceptance that the authenticated tailnet endpoint then equals shell access. Because this mode is available, the transport hardening below (per-message WS authz, non-empty allowlist required, loopback/tailscale-only bind) is **load-bearing, not optional**.
  - **`always` grants are fingerprinted**, not tool-wide: key = `(tool, fingerprint)` — for `Bash` the first token + cwd, for `Edit`/`Write` the absolute path. Grants carry a TTL (~1h), are revocable in-session (a visible "granted permissions" list), and persist in the session row so they survive driver restart/rotation.
- **Output safety (XSS).** `react-markdown` with **no `rehype-raw` and no `dangerouslySetInnerHTML` anywhere**. Bash stdout/stderr, Read content, and `toolCall.input` render as escaped text (ANSI stripped or via an escaping converter), never markdown. Required test: inject `<img onerror>` / `[x](javascript:)` into each block type and assert no execution.
- **Secret hygiene.** The redactor runs **server-side** over every `textDelta`/`thinkingDelta`/`toolResult.payload` *and* the JSONL replay path before anything leaves the host. Extend `_REDACT_PATTERNS` with the OAuth refresh-token + `ccswitch_session` cookie shapes. The shared `projects/` transcript dir is a credential-bearing artifact → chmod `0700`.
- **Input validation.** `git worktree add … -- <branch>` and reject branches beginning with `-`; validate `account` against known config dirs; validate `repo` as `^[\w.-]+/[\w.-]+$`; `Path.resolve()` + assert containment in `clones_base()`/`worktrees_base()`.
- **Transport hardening.** Cap inbound WS frame size (~256 KB); cap queued-prompt count per session; rate-limit `setPermissionMode`. Cookie `secure=True` by default (tailscale serve = HTTPS); require a non-empty `tailnet_allowlist` outside dev (refuse to boot otherwise); bind uvicorn to the tailscale/loopback interface, never `0.0.0.0`.
- **Audit.** Emit redacted audit events for `permission.allow|deny|always` (tool + input shape + identity + sessionId), `permission_mode.change` (old→new + identity), and `interrupt`.

## Review resolutions (8-persona pass, folded into the design)

**Architecture**
- **Single source of truth on attach:** tag every normalized event with the SDK message `uuid`; the JSONL replay emits the same uuids; the reducer upserts by id so a mid-turn live event that overlaps replay merges instead of duplicating. Replay skips a trailing incomplete JSONL entry and lets the live stream re-emit it.
- **Permission state lives in the orchestrator**, not the driver, so rotation can cancel (deny) and re-emit pending requests with the *same* `permId` on the new driver.
- **Atomic migration:** delete every `orchestrator.manager` call site in `ws.py` in the same change; no "driver for new / manager for old" hybrid, even temporarily.
- `trust_project(new_account, cwd)` runs before starting the rotated-to driver. `claude_session_id` is read from the SDK (first system message), not the mtime scan. Tool names map to a ccswitch `toolKind` enum so SDK names never cross the seam.

**Performance**
- Reducer keeps prior `Turn` objects referentially stable; only the streaming `Turn` gets a new reference each flush; `Turn`/blocks are `React.memo` + keyed by `turnId`/`toolId` (not array index).
- **Markdown parse + Shiki highlight run once at `turnEnd`** (plain `<pre>` while streaming). Shiki pinned to ~8 langs + one theme, lazy-loaded with a plain-text Suspense fallback.
- **Replay = completed-turn summaries**, not historical deltas; cap to the last N turns ("load earlier" for the rest); JSONL read via `asyncio.to_thread`.
- Backend batcher coalesces multiple events per 16 ms window into **one WS frame (array)**; client applies the array in one synchronous loop → one render. `DiffView` caps large diffs (first/last 100 lines + "show all").

**Frontend contract**
- Reducer state adds `openBlockId`: `toolCall` closes the open text/thinking block; next `textDelta` opens a new one. `permissionRequest` and `question` carry `turnId`. `status` is structured `{state, count}` (no `"queued:3"` string-parsing). The `SessionSocket` `EventMap` + `_handleInbound` switch are extended with all new events as part of this change (not deferred). Reattach re-emits open cards with their original ids; reducer upserts by id.

**Accessibility**
- `permissionRequest`/`question` arrival **moves focus** to the card's primary control (they block; aria-live alone is insufficient); focus returns to the composer on resolve. Question options use radio/checkbox semantics (single/multi). Thinking toggle is a `<button aria-expanded aria-controls>` with the region `hidden` when collapsed. aria-live mirror is suppressed during replay (announce "history loaded" on `replayComplete`). Errors → `role="alert"`. All new buttons keep the `min-h-[44px]` + focus-ring pattern (note: `ui/button.tsx` default size is 40px — bump for touch-critical cards).

**UX**
- On `replayComplete`, if a permission/question is open, force-scroll to it + show a sticky "Action needed" banner. Permission buttons stack vertically ≥48 px on narrow viewports; primary action is dominant. Queued prompts persist in the session model, render as muted bubbles, survive session-switch/reconnect, and are cancelable. Rename the toggle to "Plan mode" (distinct icon) + a permission-mode chip in the StatusBar opening a bottom sheet. `rotated` event carries `reason` + whether the turn was restarted ("no tools were committed"). Composer shows "Queue" (not "Send") while working. Empty-session and all-accounts-limited states get explicit treatments (the latter shows the reset time, calm framing, no urgency copy).

**QA / testing**
- Pin a **fake-SDK contract snapshot** (a `Protocol`/`ABC` both the real adapter and the fake implement); hand-authored JSONL fixtures in `server/tests/fixtures/` validated against the documented event union (no quota burned).
- Each feasibility spike gets a **binary pass/fail criterion + time-box + which tests are conditional** on it (Spike 1 gates the whole driver design).
- Required tests: permission-Future races (allow/deny/always, interrupt/disconnect → deny, double-response, response-after-resolve), multi-concurrent permissions, reconnect re-emit ordering vs `replayComplete`, queue cancel + interrupt-drains-queue, rotation-with-open-card. Enumerate reducer cases (delta into open turn, delta after `turnEnd`, orphan `toolResult`, unresolved permission on `turnEnd`, replay→live transition). Add `data-testid`s to all interactive elements; set `clearMocks: true` in `web/vitest.config.ts`.
