# Desktop-parity D-16b: MCP per-tool toggles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-tool enable/disable for MCP servers:
- a server-level default per tool in Settings › MCP;
- a per-session override in the session Context card's MCP list.

Both sit beside the existing server-level scope (`server/mcp_scope/`).

**Facts (binding):** `docs/superpowers/specs/2026-11-04-d16-verification.md` §2 (CLI 2.1.318).

**Owner decision D4 (2026-11-04):** (a) live, overridable.
- Every per-tool deny (Settings default ⊕ session override) is applied with `apply_flag_settings` right after `initialize`, and awaited before the first prompt.
- A session can re-enable a tool that the Settings default disabled.
- Per-tool denies never go into the spawn `--settings`. Spawn denies are a floor that can't be lifted live (§2.1).

**Decision:** D15 ("MCP per-tool toggles, per server and per session"). Spec row: `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` ~366.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest; React + TS, vitest, Playwright.

## Global Constraints

- **Engines.** Claude only. Codex has no deny-rule channel. Hide the per-tool switches for Codex sessions, and the Settings tool list says "Claude sessions".
- **One flag-`permissions` composer.**
  - `apply_flag_settings` replaces a top-level key wholesale (§2.1 merge semantics). Add ONE composer, e.g. `flag_settings.compose_permissions(session) -> dict | None`. It owns the entire `permissions` object ViberPit sends, and is the only code allowed to put `permissions` in a flag patch.
  - Today it builds `{deny: [mcp__<server>__<tool>, …]}`.
  - Document in its docstring that any future `permissions` key (e.g. `additionalDirectories` for `/add-dir`) must be added here.
  - Add a test that greps for or asserts no other `apply_flag_settings` caller sends `permissions`.
- **Applying.**
  - After `initialize`, next to `_enforce_disabled_mcp` (`claude_driver.py:~2698`), send `apply_flag_settings({"permissions": composed})`, and await it before the first prompt is written. When the effective deny set is empty, send nothing.
  - **Live toggle:** recompose and send the whole object. `deny: []` restores every tool (§2.1).
  - **Respawn / daemon restart:** reapplied from persisted state. Never put it in spawn `--settings`.
  - **Errors:** map them through the existing D-14 `SettingsError` / codes table. Expected refusals are logged at INFO.
- **Persistence.**
  - Extend the existing `mcp_scope` store (`server/mcp_scope/store.py`) with a tool dimension. Read the store first, and reuse its table shape if it is keyed by `(scope, server[, session])`: add a nullable `tool` column, where NULL means the server-level row and is unchanged. If the shape doesn't fit, add a new model `McpToolRule`, which means updating `_REQUIRED_EXTRAS` / `RECONCILE_EXEMPT` and running the Postgres lists. Record the choice in the report.
  - There are two levels:
    - the **server default per tool**, set in Settings;
    - the **session override per tool**, set from the Context card.
  - A per-repo tool level is **out of scope**; ruling: YAGNI.
  - Effective deny for a session = (default-disabled tools ∖ session-enabled overrides) ∪ session-disabled overrides.
  - A server that is disabled at server level (existing scope) contributes no tool denies; it is already off.
  - Migration `0103`: chain it off the newest revision on origin/master, renumbering if needed.
- **Tool lists.**
  - `sanitize_statuses` (`server/mcp_health/status.py:~97`) and `McpServerState` (`:~33`) keep `tools: [{name, description, annotations: {readOnly, destructive, openWorld}}]`, in addition to `tool_count`.
    - Cap: at most 200 tools per server, with descriptions truncated to 300 chars, so a pathological server can't bloat payloads.
    - `mcp_status` with `liveReply` may fall back to `toolCount` only. In that case the switches are hidden and show "Tool list unavailable".
  - **Session.** The rail lists tools from the session's live `mcp_status`, plus effective state, including the `get_context_usage.mcpTools` read-back where cheap.
  - **Settings** has no live session. It uses the last-seen tool list per server, persisted from `probe_mcp` (`POST /library/mcp/test`) and from live session polls (`mcp_health/store.py`). Show "Seen <relative time>" and an empty-state hint that says "Test the server to load its tools".
- **API.**
  - `PUT /api/library/mcp/{server}/tools/{tool} {enabled}`: the Settings default.
  - `POST /api/sessions/{id}/mcp/{server}/tools/{tool} {enabled}`: the session override, applied live.
    - It runs per session through RPC, remote, fleet and facade, the same way the existing `/mcp/{server}/toggle` route does; follow that route exactly.
    - Errors:
      - 409 `daemon_outdated`;
      - 409 `MCP_TOOL_UNKNOWN` when the tool is not in the server's current list;
      - 409 `MCP_TOOLS_UNAVAILABLE` for Codex or when the list is unknown.
  - The session payload, or the existing session MCP endpoint, gains per-server `tools[]` with `{name, description, annotations, enabled, source: "default"|"session"}`.
  - **Human gate.** Both mutation routes go through the existing human-gate helper (`human_sender` / `require_human`, as in #3291/#3292), and the session route through `session_actions`, with audit `session.mcp_tool`. Disabling a tool narrows what the session can do, but enabling widens it, so gate both directions for simplicity. Add a test.
- **Gates.**
  - Server: ruff, mypy, focused pytest, parity tests, and the full `server/tests -n 4` in a clean env before the PR. Postgres lists if there is a new model.
  - Web: typecheck, lint, full vitest, build, budget, e2e.
  - A no-model CLI contract test `test_mcp_tool_deny_cli_contract.py` (`slow`; skip without the binary; throwaway config dir; a stdio probe MCP server, see how the spike or the D-13 contract tests built one) asserts:
    - a live deny removes the tool from `get_context_usage.mcpTools`;
    - `deny: []` restores it;
    - `mcp_status` still lists all tools.

## Review Focus

1. **Live re-enable:** Settings disables `tool_b`, and the session re-enables it live. The tool is back in `mcpTools` (Task 1 contract and unit tests).
2. **Daemon restart with overrides:** the respawn reapplies the composed denies after `initialize`, before the first prompt (Task 1).
3. **Server-level disabled plus per-tool rows:** no stray denies are sent, and re-enabling the server restores the per-tool state (Task 1).
4. **Codex session, or `toolCount`-only status:** no switches, no errors (Task 2).
5. **Composer exclusivity:** no other code path sends a flag `permissions` key (Task 1 test).

---

### Task 1: Server — tool lists, per-tool store, composer, live apply, routes

**Files:**
- `mcp_health/status.py` and `store.py` (tools carried and persisted);
- the `mcp_scope` store, plus migration `0103`;
- `flag_settings.py` composer;
- the driver post-initialize apply;
- orchestrator `set_mcp_tool` / recompose;
- RPC, remote, fleet and facade;
- `session_actions`;
- both routes and the payload.

**Tests:**
- `server/tests/test_mcp_tool_toggles.py`
- the contract test
- schema reconcile and the Postgres lists if a new model was added

- [ ] **Step 1: failing tests.** Cover every bullet above, plus Review Focus 1, 2, 3 and 5.
- [ ] **Step 2–4.** Run focused tests, parity, ruff and mypy.
- [ ] **Step 5: Commit** `feat(mcp): per-tool enable/disable — Settings default and live session override (D-16b)`

### Task 2: Web — tool switches in Settings › MCP and the Context card

**Files:**
- `settings/sections/McpSection.tsx`: a server row expands to tool switches, with readOnly/destructive badges from annotations and "Seen …".
- `sessions/McpServerList.tsx` / `rail/ContextCard.tsx`: a server row expands to tool switches, plus a "Session" chip when overridden.
- `api/mcpScopeHooks.ts`: hooks.
- `types.ts`.
- Mocks.

**Tests:** unit tests, and e2e `web/e2e/mcp-tools.spec.ts` (route-mocked, 1440 and 390).

- [ ] **Step 1: failing tests.**
  - The switches render from `tools[]`.
  - Toggling posts the right route.
  - The session override chip appears.
  - Hidden for Codex, and when the list is unavailable (Review Focus 4).
  - The Settings empty state.
  - Destructive and readOnly badges.
- [ ] **Step 2–4.** Full web gates and e2e.
- [ ] **Step 5: Commit** `feat(web): MCP per-tool switches in Settings and the session Context card (D-16b)`

### Task 3: Benchmark and docs

- Benchmark: `MCP servers + per-tool toggles` → Y. Regenerate §3 and report the target (D-16a: 93%).
- Spec §8/§10/§11:
  - the composer rule (single owner of flag `permissions`);
  - D4 live-overridable;
  - no repo level;
  - tool-list sources.

  Mark D-16b.
- Product docs: the MCP feature doc.
- Ceilings: measured + 0.3 only if moved.
- [ ] Commit `docs: D-16b benchmark rerun and MCP per-tool docs`.
