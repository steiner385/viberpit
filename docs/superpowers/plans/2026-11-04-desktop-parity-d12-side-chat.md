# Desktop-parity D-12: The Assistant as a session-scoped side chat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inside a session, the Assistant drawer is a side chat about that session. It reads the session's transcript, diff and search results, and it can never write to that session. ⌘; and `/btw <text>` open the drawer. Outside a session the Assistant is unchanged. There is no second chat surface.

**Architecture:**
- **Already there.** ⌘;/Ctrl+; opens the drawer (`app.assistant`). The drawer already sends `{route, sessionId}` with every prompt, rendered as `<viberpit-context session=…>` (`server/assistant/context.py`, `claude_driver.py:1295`).
- **What D-12 adds:**
  - **Enforcement in the tool layer.** It lives in the in-process MCP server, which runs in the daemon next to the driver. While the driver's current context names session X, every MUTATION or DESTRUCTIVE tool whose target includes X is refused before its HTTP call.
  - **Two READ tools:** session diff and in-session search.
  - **A static system-prompt line** describing side-chat behaviour.
  - **The `/btw` app command**, which opens the drawer and sends the text.

**Tech Stack:** Python, FastAPI, pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2 ("Assistant drawer as side chat" `[A-D18]`), §8 (app slash commands), §11 D-12. Decision D18. Code map: see the ledger's `repo-notes.md`. Assistant rules: `CLAUDE.md` § AI assistant, and `docs/product/features/ai-assistant.md`.

## Global Constraints

- **Scope source.**
  - The scoped session is the `sessionId` of the latest validated context the driver sent for this assistant thread. Today that is the context header's `session` attribute (`claude_driver.py` tracks the last one it sent).
  - Expose it read-only to the tool layer as a callable, `current_scope() -> str | None`, passed to `build_server`. Never let tool input set it.
  - With no sessionId in the context, there is no scope and behaviour is unchanged.
- **Enforcement.**
  - `build_tools.guarded` (`server/assistant/tools.py`) gains a scope check that runs before the handler. For any tool whose class is MUTATION or DESTRUCTIVE, if `current_scope()` is X and the tool's target set contains X, the handler is not called. It returns a tool error whose code is `SCOPED_READ_ONLY` and whose message is **`This chat is about session X and can't change it — ask in the session itself, or close the side chat`**. It logs at INFO.
  - The target set comes from one helper, `tool_targets(name, input) -> set[str]`. It covers `session_id`, `id`, and `ids` / `session_ids` lists for the batch tools, plus `attach_feedback_to_session`'s target. A test enumerates **every** MUTATION/DESTRUCTIVE tool in `TOOL_SPECS`. Each one must either be covered by `tool_targets` or appear in an explicit `NO_SESSION_TARGET` set with a reason (for example `create_session`). A new tool fails the test until it has been classified.
  - Tools targeting other sessions are unaffected. READ tools are unaffected.
- **New READ tools.** Each calls exactly one endpoint, and has an allowlist row plus `test_route_allowlist` entries:
  - `get_session_diff(session_id, summary=true)` → `GET /api/sessions/{id}/diff?summary=1` (D-4).
  - `search_session_transcript(session_id, q, limit<=50)` → `GET /api/sessions/{id}/search` (D-11). Output is already redacted.
- **System prompt.** Add one static sentence to `ASSISTANT_SYSTEM_PROMPT`, with no braces: "When the context names a session, you are a side chat about it: answer from its transcript, diff and search, and never act on it." Update the prompt tests.
- **Docs and evals.**
  - Update `ai-assistant.md`: tool totals (112 → 114, default 105 → 107), the pack table, and a "Side chat" section.
  - Extend `scripts/eval_assistant.py` so a case can carry a `session_id` for the context header.
  - Add at least 4 eval cases:
    - a diff question → `get_session_diff`;
    - "where did we discuss X" → `search_session_transcript`;
    - "send it a prompt to continue" while scoped → no `send_prompt` to that session (`forbid_tools`);
    - an unscoped control.
  - Update the case-digest and count tests (memory: parallel assistant-tool PRs conflict on counters; rebase-check them).
- **`/btw`.**
  - An APP client command in `catalog.py` with `argument_hint="<question>"`, available in coding sessions only. It is excluded from the assistant's own catalog: make `build_catalog` kind-aware, with the kind passed in from `orchestrator.py:23611`.
  - **Web:** `useBtwCommand` is chained in `LiveComposer` like `/rewind`. `/btw` alone opens the drawer. `/btw <text>` opens it and **sends** `<text>` as the next drawer prompt.
  - Opening and sending goes through a new single channel, `shell/assistantGestures.tsx`: `requestAssistant({send?: string})`. `AssistantSheet` consumes it once its session exists. If the session already exists but a turn is running, it queues through the normal send path. The channel is never driven by URL parameters.
  - The server-side dispatcher fallback (`/btw` reaching the server as a first prompt) answers with a notice.
- **Drawer scope indicator.** While a session is open, the drawer header shows `About: <session title>` with the session's status dot, and starter chips stay session-scoped (existing). Outside a session, nothing changes.
- **The kill switch** `VIBERPIT_ASSISTANT_ENABLED` still governs everything. With it off, `/btw` is hidden from the catalog.
- **Gates.**
  - Server: ruff, mypy, the assistant test suites (`test_assistant_*`, `test_eval_assistant_*`, `test_command_catalog`, `test_driver_context_header`).
  - Web: typecheck, lint, full vitest, budget (ceilings 106.7 / 300.5), and e2e.

## Review Focus

1. **The scope switches mid-conversation.** The user opens the drawer in session A, asks something, then navigates to session B and asks "archive A". The scope is now B, so archiving A is allowed. Asking "archive B" is refused. Pinned in Task 1.
2. **Batch tools containing the scoped id** (`archive_sessions([X, Y])`) are refused whole, never applied partially. Pinned in Task 1.
3. **`/btw` while the drawer's session is still being created:** the text is sent once the session exists, never dropped and never sent twice. Pinned in Task 2.
4. **`/btw` typed inside the drawer's own composer:** it isn't offered there. Pinned in Task 2.
5. **Operator tier on:** the operator tools are classified, and scope still blocks writes to X. Pinned in Task 1.

---

### Task 1: Server — scope enforcement, read tools, prompt, evals

**Files:**
- Modify:
  - `server/assistant/tools.py`: `tool_targets`, `NO_SESSION_TARGET`, the scope check in `guarded`, and the two READ tools.
  - `server/assistant/server.py`: `build_server(..., current_scope=None)`.
  - `orchestrator.py`, at the overlay build around line 2100–2153: pass a callable reading the driver's last context sessionId.
  - `claude_driver.py`, if the last context isn't already exposed.
  - `server/assistant/policy.py`: allowlist rows.
  - `server/assistant/prompt.py`.
  - `docs/product/features/ai-assistant.md`.
  - `scripts/eval_assistant.py` and `docs/superpowers/evals/assistant/cases.jsonl`.
  - The counter tests.
- Test: `server/tests/test_assistant_scope.py`, plus the existing suites.

- [ ] **Step 1: Write failing tests.**
  - With a scope of X, `send_prompt(X)` is refused with `SCOPED_READ_ONLY` and the HTTP call is never made (assert with a fake API).
  - `archive_sessions([X, Y])` is refused as a whole.
  - `send_prompt(Y)` is allowed.
  - READ tools on X are allowed.
  - With no scope, nothing is refused.
  - The scope changes from A to B, which is Review Focus 1.
  - Every MUTATION/DESTRUCTIVE tool, operator tools included, is classified.
  - The new tools' endpoints and allowlist rows are correct, and the diff tool's output is passed through.
  - The prompt contains the side-chat sentence and still has no braces.
  - The harness renders a case's `session_id` into the context header.
  - The doc totals are correct.
- [ ] **Step 2–4.** Run them, then the assistant suites, ruff and mypy.
- [ ] **Step 5: Commit** `feat(assistant): session-scoped side chat — read tools and write guard (D-12)`

### Task 2: Web — `/btw`, open-and-send channel, scope indicator

**Files:**
- Server: `catalog.py` (the `/btw` entry, kind-aware gate) and `dispatcher.py` (the notice).
- Create: `web/src/shell/assistantGestures.tsx` and `web/src/features/assistant/useBtwCommand.ts`.
- Modify: `PromptDock.tsx` (chain), `AssistantSheet.tsx` (consume the channel, header indicator), and the tests.
- e2e: extend the assistant drawer e2e if one exists (grep `web/e2e` for assistant); otherwise create `web/e2e/side-chat.spec.ts`, route-mocked.

- [ ] **Step 1: Write failing tests.**
  - `/btw` alone opens the drawer and sends nothing.
  - `/btw why is it failing` opens the drawer and sends that text once. This holds when the assistant session already exists, and when it's still being created (Review Focus 3).
  - `/btw` doesn't appear in the drawer's composer catalog (Review Focus 4).
  - The header shows `About: <title>` inside a session and nothing outside one.
  - With the kill switch off, `/btw` is hidden.
  - ⌘; behaviour is unchanged.
  - The catalog and dispatcher tests pass.
- [ ] **Step 2–4.** Run the full web gates, budget, and the e2e; then the server catalog tests.
- [ ] **Step 5: Commit** `feat(web): /btw opens the Assistant side chat about the current session (D-12)`

### Task 3: Benchmark and docs

- **Benchmark** (`scripts/desktop_parity_benchmark.py`): update the side-chat row and the `/btw` part of the app-command row. Regenerate audit §3 and report the target (D-11 was 87%).
- **Spec §2 and §8:** record the enforcement point and the message, the read tools, and the `/btw` semantics.
- **Product docs:** `ai-assistant.md` and `web-dashboard-mobile.md`.
- **Ceilings:** set them to measured + 0.3 only if the measurement exceeds them, under a controller ruling.
- [ ] Commit `docs: D-12 benchmark rerun and side-chat docs`.
