---
slug: session-tools
title: Session Tool Surface (session_context, report_feedback)
state: delivering
links:
  specs:
    - docs/superpowers/specs/2026-09-06-session-feedback-to-backlog-design.md
  code:
    - server/mcp_spec.py
    - server/sessions/tools/registry.py
    - server/sessions/tools/tools.py
    - server/sessions/tools/context.py
    - server/sessions/tools/server.py
    - server/sessions/tools/instruction.py
    - server/sessions/tools/codex.py
    - server/sessions/feedback.py
---

# Session Tool Surface (session_context, report_feedback)

**Problem** — A coding session run by ccswitch has no way to ask "what am I, and why was I
dispatched" or to tell ccswitch itself (not the repo it's working in) that something about the
harness is broken — a wrong dispatch prompt, a contradictory instruction, a mistimed model switch.
Today that only reaches a human as a stuck session or a bad PR, well after the fact.

**Target users** — every coding session ccswitch runs (Claude and, on fresh threads, Codex), and
the operators who triage `session_feedback` rows into the backlog.

**Desired outcome** — every coding session mounts an in-process `ccswitch` MCP server with two
tools: `session_context` (id, name, repo, branch, cwd, host, engine, model, account, dispatch
origin/ref, effective goal, permission mode) and `report_feedback` (category, impact, title, body,
optional evidence — one call per distinct problem, at most `session_feedback_cap` per session).
Both are `ToolClass.READ` in `server/mcp_spec.py`'s shared registry — read-only in effect, since
neither changes anything the user can see — so `PermissionRegistry` resolves them silently in
every session mode, never raising a permission card (pinned by
`server/tests/test_permissions_session_tools.py`). Handlers close over a `SessionToolContext` built
in the daemon process that owns the session — no loopback HTTP call, no bearer token — so the
surface works identically on any fleet host, including rocky.

**Success metrics** — a session can answer "how was I dispatched" and file a feedback row without
leaving its own turn; `server/tests/test_session_tools_real_binary.py` (gated on
`CCSWITCH_TEST_CLAUDE_MCP=1`) verifies against the real `claude` binary, per Constitution III, that
a project's own `.mcp.json` servers and `ccswitch` coexist and `report_feedback` raises no prompt.

**Non-goals (Phase 1)** — filing to GitHub (Phase 2's automation), an API/UI/assistant view of
`session_feedback`, and `session_url` in the context snapshot (needs the edge's public URL, known
only to the Phase 2 filer).

**Notes** —
- **Kill switch** — `CCSWITCH_SESSION_TOOLS_ENABLED` (default `true`); off drops the overlay at the
  next session build, no in-flight session is torn down.
- **Feedback row lifecycle (Phase 1)** — `record_feedback` (`server/sessions/feedback.py`) dedupes
  exact repeats by `(session_id, fingerprint)` → `status="duplicate"`, else `"pending"` or
  `"capped"` against `session_feedback_cap`; Phase 2 adds `"filed"`/`"merged"`/`"throttled"`/
  `"dismissed"` as the automation takes rows off `pending`.
- **Codex resume gap** — Codex declares the two tools as `dynamicTools` on a fresh `thread/start`
  only; `thread/resume` silently drops them (no schema field, no rejection), so a resumed Codex
  thread has none until respawned. Confirmed against codex-cli 0.153.2 in
  `docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md`.
