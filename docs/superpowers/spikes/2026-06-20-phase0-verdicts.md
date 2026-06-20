# Phase 0 spike verdicts (2026-06-20)

Run via `claude-agent-sdk==0.2.105` against idle accounts (`primary`, `secondary`) with bounded timeouts. Script: `tmp/spike.py` (not committed).

## Gate 0.1 — SDK OAuth + persistent streaming: **PASS**
- The SDK spawns `claude` using `CLAUDE_CONFIG_DIR` (composed per-account dir) and authenticates with the Max OAuth — **no API key**. Real reply received.
- Persistent multi-turn confirmed: two `query()` calls on one `ClaudeSDKClient` returned `PONG` then `PING`.
- `include_partial_messages=True` yields live `StreamEvent`s.

## Gate 0.2 — Resume across account config dirs (rotation): **PASS**
- Captured `session_id` from turn 1 under `primary`, opened a NEW `ClaudeSDKClient(options=ClaudeAgentOptions(resume=session_id, env=CLAUDE_CONFIG_DIR=<secondary>))`, asked what the first word was → answered **`PONG`**. History survived the account switch (shared `projects/` transcript). **Approach A rotation is valid; no fallback to Approach B needed.**

## Gate 0.3 — AskUserQuestion in SDK mode: **PASS (fulfillable)**
- The model emitted a `ToolUseBlock` with `name == "AskUserQuestion"`, and it routed through the `can_use_tool` callback (`tools_seen == ["AskUserQuestion"]`). So ccswitch can intercept it, emit a `question` event, and (Phase 3) return the selection. v1 may still ship the answer path incrementally.

## Observed message/stream contract (authoritative for Phase 1)

`ClaudeSDKClient.receive_response()` yields, in order, objects of these types:
- `HookEventMessage` — ignore.
- `SystemMessage` — `.subtype == "init"` carries session metadata; `.data` dict. (Also other subtypes.)
- `StreamEvent` (only with `include_partial_messages=True`) — `.event` is the raw Anthropic stream event dict:
  - `{"type":"message_start"}`
  - `{"type":"content_block_start","index":i,"content_block":{"type":"text"|"thinking"|"tool_use", ...}}`
  - `{"type":"content_block_delta","index":i,"delta":{"type":"text_delta","text":...}}` (also `thinking_delta`→`.thinking`, `input_json_delta`→`.partial_json`)
  - `{"type":"content_block_stop","index":i}`
  - `{"type":"message_delta"}`, `{"type":"message_stop"}`
- `AssistantMessage` — `.content: list[ContentBlock]` where blocks are `TextBlock(text)`, `ThinkingBlock(thinking)`, `ToolUseBlock(id, name, input)`. Emitted once per assistant message (the consolidated form).
- `UserMessage` — `.content` includes `ToolResultBlock(tool_use_id, content, is_error)` for tool outputs.
- `RateLimitEvent` — surfaces rate-limit state (useful for the usage/rotation path).
- `ResultMessage` — `.session_id`, `.usage`, `.total_cost_usd`, etc. Marks turn end.

**Permissions:** `ClaudeAgentOptions(can_use_tool=async fn(tool_name, tool_input, ctx) -> PermissionResultAllow|PermissionResultDeny)`. `set_permission_mode(mode)` and `interrupt()` exist on the client. `resume=<session_id>` on options resumes (even under a different config dir).

## Plan impact
- Task 1.2/1.3 (fake SDK + translator): build against the **real** types above — drive text/thinking *deltas* from `StreamEvent.content_block_delta`, tool calls from `AssistantMessage` `ToolUseBlock`, tool results from `UserMessage` `ToolResultBlock`, turn end + `session_id` from `ResultMessage`/`SystemMessage(init)`. (Supersedes the placeholder `assistant_text_delta`/`tool_use` fixture shapes in the plan.)
- `SessionDriver.start()` reads `session_id` from `ResultMessage`/`SystemMessage`, not a filesystem scan (already in plan).
- Driver factory: `ClaudeSDKClient(options=ClaudeAgentOptions(cwd, env={...,CLAUDE_CONFIG_DIR}, permission_mode, resume, can_use_tool, include_partial_messages=True))`.
