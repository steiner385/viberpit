# ccswitch AI Assistant — Capability Audit and v2 Design

**Date:** 2026-09-05
**Status:** Proposed — awaiting review (produced in an autonomous session; every decision taken
without a human is listed in §0.3 so it can be overridden in review)
**Extends:** `2026-09-04-ai-assistant-design.md` (v1, delivered — Phases 0–4, epic #1052). Decisions
A1–A8 of v1 stand unchanged. This spec **amends A9** (§4.8) and **supersedes v1 §2 "Capabilities,
v1"** as the tool-surface roadmap. Everything else in v1 (runtime, token, permission posture,
context chip, lifecycle, events) is unchanged and is referenced, not restated.
**Research basis:** `2026-09-04-ai-assistant-research.md` (norms, gap register §6).
**Implementation plan:** `docs/superpowers/plans/2026-09-05-ai-assistant-capabilities-v2.md`.
**Tracking:** epic issue created from the plan; every pack below is one phase tracker.

---

## 0. Summary

### 0.1 One paragraph

The assistant shipped as designed: a tool-restricted `claude` session in a drawer, 28 tools over
ccswitch's own HTTP API, mutations behind permission cards, a 40-case eval at 98 %. The audit in
§1 finds it covers the **core** use cases it was built for (fleet triage, session diagnosis and
steering, PR/CI diagnosis, quota and host awareness) and a set of **extended** ones (automation
visibility, archive management, PR readiness, model approvals). It also finds fifteen concrete
gaps (§1.5): four starter chips with no backing tool, no repo discovery for "start a session on
X", no notion of the current time, no transcript detail, no batch actions, no product/automation
control, no proactive channel, no external client access, and a Claude-only runtime in a product
that now has two engines. v2 closes those gaps as nine **capability packs** (§4), each a
self-contained set of tools + allowlist rows + eval cases + starters, delivered in six phases.
The v1 rules hold for every pack: a tool is a thin adapter over one endpoint; reads run silently,
mutations get a card, destructive tools can never be always-allowed; nothing the assistant does is
invisible to the audit log.

### 0.2 What "support everything" means here

Everything the **web UI can do with the user's identity** should be askable in the drawer, in one of
three shapes: (a) a tool, (b) an accurate "that lives in Settings → X" redirect for the few surfaces
that stay excluded for safety, or (c) a recorded capability gap (`report_gap`, §4.1) so that the
things people ask for and cannot get become a metric instead of an anecdote. "Everything" does
**not** mean the assistant writes code, edits files, browses the web or manages credentials — those
non-goals from v1 §1 stand (§3).

### 0.3 Decisions taken autonomously (override in review)

| # | Decision | Why this and not the alternative |
|---|---|---|
| B1 | **Operator-tier tools** (deploy start, host upgrade, account refresh/enable, re-auth kick-off) ship **opt-in, default off**, as class D, and are **not registered** with the MCP server when the setting is off | v1 A9 excluded them because of blast radius. Opt-in keeps the default posture identical to today while letting a single-operator deployment turn them on. Not registering (rather than registering-then-denying) means the model cannot even attempt them, and the allowlist rows are conditional on the same setting — one switch, two enforcement points. |
| B2 | **Proactive = notices, not model turns.** A watch fires a *notice card* into the drawer (plus web push); the model never speaks unprompted | v1 non-goal "no unsolicited messages" stays true to the letter; the user still gets "session X finished" where they are looking. A notice carries an "Ask about it" chip that turns into a normal prompt. |
| B3 | **External access = the same tool table, two adapters:** a stdio MCP server (`ccswitch mcp`) for other agents, and a one-shot CLI (`ccswitch ask`) for scripts. Read-only by default; mutations need an explicit flag and mint an audit `via="mcp-external"` | Research §3 already anticipated exposing the in-process server externally. Re-using `build_tools` means zero new tool logic; the only new code is a **user-scoped token** (`ccas2:`) that is *not* loopback-bound and is revocable. |
| B4 | **Batch tools produce one card that enumerates every target by title**, capped at 20 | "Archive the two that are done" producing two cards is v1 F3 behaviour; one card per intent is what the user asked for. The cap bounds the blast radius of a mis-scoped filter. |
| B5 | **GitHub write surface is limited to `comment_on_pr` and `create_issue`**, both class M, both stamped "AI-generated via ccswitch assistant" | These are the two writes an operator actually wants from a triage conversation ("file this", "tell the PR author"). Merging, closing, labelling and reviewing stay with the existing `ready_merge_pr` and the coding sessions. |
| B6 | **Current time goes in the tool-result envelope, not the system prompt** | The prompt is the cache prefix and must stay frozen (v1 §4.5). Tool results are never cached, so `"now": <iso>` there is free and lands exactly when the model reasons about "last hour". |
| B7 | **Engine neutrality is a spike, not a commitment** | The Codex driver's MCP support is unverified (constitution III). The spike answers one question: can the Codex engine mount an in-process MCP server? Yes → the overlay moves behind `engine_policy`; no → the status endpoint reports `engineUnavailable` and the drawer explains. |
| B8 | **Single thread per identity stays** | Still one operator per deployment; v1 §10 Q1 remains open for the reviewer. |
| B9 | **Phase order: fix → product/automations → session power + GitHub → proactive → operator/external → UX/feedback** | Fixes first because two are user-visible defects (chips that cannot work). Product and automation packs next because they are the largest bodies of *existing* app capability the assistant cannot see. Operator tier late because it needs the opt-in plumbing and the most review. |

---

## 1. Audit — the assistant as built (2026-09-05, `origin/master` 746d18dd)

### 1.1 Surfaces and interaction

| Capability | Where | State |
|---|---|---|
| Right drawer from every section, `?drawer=assistant`, `Cmd+;` / `Ctrl+;` | `shell/regions/DrawerHost.tsx`, `shell/shortcuts.ts` | live |
| Push column ≥ 1280 px, overlay below, full-height sheet on phone | `DrawerHost` | live |
| Hidden in kiosk mode | `app.drawer` row of the layout table (target-state §) | live |
| `Esc` stops a live assistant turn before closing the drawer | `assistantGestures.tsx` + `DrawerHost` | live |
| Starter chips per nav section + session-open set | `features/assistant/starters.ts` (`Record<NavId,…>`, exhaustive) | live — **but see G1** |
| Context chip `{route, sessionId}` on every prompt, replay-safe | `server/assistant/context.py`, `SessionComposer.contextProvider` | live |
| Streaming, Stop, partial retention, queued/thinking/error states | existing session reducer + `TranscriptView variant="assistant"` | live |
| Voice input (server-side Whisper, Web Speech fallback) | shared `Composer` → `MicButton` | live (inherited) |
| Image attachments | shared `Composer` attachments → the model sees them natively | live (inherited); no tool reads them |
| CLI slash commands relayed to the composer (`rt.commands`) | `useSessionRuntime` | live (inherited) — surface unaudited for the assistant kind |
| Model name shown; **no picker** | `AssistantHeader.tsx` | label only — **G2** |
| "New chat" with confirmation | `AssistantHeader.tsx` → `POST /api/assistant/session {fresh:true}` | live |
| Disclosure footer "AI-generated. Actions ask for confirmation." | `AssistantSheet.tsx` | live |
| `aria-live="polite"` transcript, dialog/complementary roles, focus trap | `AssistantSheet`, `DrawerHost` | live |
| Disabled / create-error / skeleton states | `AssistantSheet.tsx` | live |
| Settings → General "AI assistant" group (enabled, model) | `features/settings/sections/GeneralSection.tsx` | live |

### 1.2 Tool surface (28 tools; `server/assistant/tools.py`)

| Class | Count | Tools |
|---|---|---|
| Read (silent) | 12 | `list_sessions`, `get_session`, `get_session_transcript`, `search_sessions`, `list_archived_sessions`, `list_accounts`, `list_hosts`, `list_prs`, `get_pipeline_health`, `list_automations`, `get_automation_events`, `get_deploy_status` |
| Mutation (card; "Always allow" honoured per thread) | 13 | `create_session`, `send_prompt`, `interrupt_session`, `answer_question`, `resolve_permission` (allow/deny only), `set_model`, `set_permission_mode`, `resolve_model_approval`, `archive_session`, `unarchive_session`, `set_autopilot_paused`, `fix_pr`, `set_automation_enabled` |
| Destructive (card in every mode; never always-allowed) | 3 | `delete_session`, `cancel_queued_session`, `ready_merge_pr` |

Every tool is a thin adapter over exactly one endpoint in `ASSISTANT_ROUTE_ALLOW`
(`server/assistant/policy.py`, 29 rows incl. `GET /api/auth/me`). Session ids are matched as
UUID4 so static collection segments (`batch`, `search`, `archived`, `adopt`) can never be
mistaken for an id.

### 1.3 Guardrails as built

| Control | Value | Source |
|---|---|---|
| Built-in tools | `tools=[]` + 17 names in `DISALLOWED_BUILTINS` (incl. `SlashCommand`, `Skill`, `ExitPlanMode`) | `options.py` |
| Other MCP servers | `strict_mcp_config=True` | `options.py` |
| Operator config isolation | scratch `CLAUDE_CONFIG_DIR` (`isolated=True`); prompt "role override" clause because `setting_sources` hangs the SDK | `options.py`, `prompt.py` |
| Token | `ccas1:` identity+session bound, loopback-only, TTL = idle × 4, never persisted | `token.py`, `auth/middleware.py` |
| Route allowlist | 403 before routing for anything unlisted; `/api/assistant/*`, `/api/settings`, `/api/accounts/*` mutations, `/api/deploy/start`, fleet mutations, batch routes all excluded | `policy.py` |
| Tool-call rate | burst 30, refill 1/s, per assistant session | `client.py` |
| Prompt rate | 120 / hour / identity | WS prompt path (#1185) |
| Turn budget | `max_turns=30`, `effort="low"`, default model `sonnet`, gated tiers → 400 `MODEL_GATED` | `options.py`, `api.py` |
| Injection framing | `{"source":"ccswitch-api","trust":"untrusted-data","data":…}`; strings cut at 2000 chars; tag-like `<` escaped | `tools.py:_scrub`, `wrap_untrusted` |
| `resolve_permission` | can never pass `always` (tool boundary + API `via` check) | `tools.py`, `session_actions.py` |
| Idle reap / resume | 900 s → `ended`; reopen resumes; adopted from DB after daemon restart (#1122); `find_assistant` proxied over daemon RPC (#1201) | orchestrator |
| Audit | `assistant.session.create/reset`, `assistant.tool_call {tool,class,ok,ms}`, `via="assistant"` on downstream events | `tools.py:_audit_tool_call` |
| Metrics | `assistant_prompts_total`, `assistant_tool_calls_total{tool,class,outcome}`, `assistant_ttft_ms`, `assistant_turn_ms`, `assistant_sessions_live` | `obs/metrics.py` (#1186) |
| Kill switch | `CCSWITCH_ASSISTANT_ENABLED` / `AppSettings.assistant_enabled` — stops **new** threads; in-flight threads live until idle reap (v1 §8) | `policy.py` — **see G9** |

### 1.4 Quality evidence

- Eval: 40 cases in `docs/superpowers/evals/assistant/cases.jsonl`, 39/40 (98 %) on `sonnet`
  (`results-2026-09-05.md`); ≥ 6 prompt-injection cases, all pass. The single failure is a rubric
  rigor miss (`pause-autopilot` did not cite verification evidence), not a safety issue.
- Live smoke: `scripts/smoke_assistant.py` (drawer → one read tool → one mutation → card answered).
- Spike evidence: `docs/superpowers/spikes/2026-09-04-assistant-sdk-findings.md`.

### 1.5 Gaps found (G-numbers are referenced by the packs in §4 and the plan)

| # | Gap | Evidence | Severity |
|---|---|---|---|
| **G1** | **Four starter chips have no backing tool.** `initiatives` ("Which initiatives have no active session?", "Summarize initiative progress") and `projects` ("Which project has the most open work?", "What is blocked by a dependency?") — no PM/SDLC tool exists; `insights` "What regressed in CI recently?" is only partly served by `get_pipeline_health`. Clicking these yields "I have no tool for that". Nothing tests starter→tool coverage. | `starters.ts` vs `TOOL_SPECS` | user-visible defect |
| **G2** | **No model picker** in the drawer despite v1 §4.6/§4.9 ("composer model menu reused"). `AssistantHeader` reads the model only; the endpoint exists (`POST /api/sessions/{id}/model`) and the `MODEL_GATED` policy path is already implemented for creation. | `AssistantHeader.tsx` header comment | spec drift |
| **G3** | **No repo discovery.** `create_session(repo=…)` needs an exact `owner/name`; `/api/github/repos` and `/api/github/branches` are not exposed, so "start a session on the billing repo" cannot resolve the name. | `tools.py`, `api/repos.py` | usability hole in a core flow |
| **G4** | **No notion of "now".** The prompt is deliberately frozen (cache prefix) and no tool result carries a timestamp, so "what did autopilot do in the last hour?" (a shipped starter) relies on the model guessing the current time from event timestamps. | `prompt.py`, `wrap_untrusted` | correctness |
| **G5** | **Transcript detail is unreachable.** `get_session_transcript` returns text and tool *names* only; tool inputs/outputs are elided. "Why did the tests fail in session X?" cannot be answered. `export.md` exists but is not exposed. | `tools.py`, `api/sessions.py` | core-flow depth |
| **G6** | **No batch actions.** `POST /api/sessions/batch/{archive,unarchive,resume,rotate,permanent-delete}` exist and are excluded; "archive the done ones" produces N cards (v1 F3). | `policy.py` comment | convenience |
| **G7** | **Automations are visible but not steerable.** `/{id}/actionable`, `/{id}/trigger`, `/{id}/sessions`, `backlog-runner/{candidates,groups,dispatch}`, `cicd-doctor/findings`, `pr-doctor/{consolidation,consolidate,jobs}` are all unexposed. "What is the PR doctor proposing and should I accept it?" is impossible. | `api/automations.py` | extended-flow gap |
| **G8** | **Claude-only runtime.** `assistant_options` is a `ClaudeAgentOptions` overlay; the Codex engine (#1157, #1203) cannot host an assistant thread. A Codex-only deployment has a dead drawer with no explanation. | `options.py`, `engine.py` | platform gap |
| **G9** | **Kill-switch semantics drift between docs.** Feature doc: "disables both the drawer's composer and the assistant HTTP routes"; v1 §8 (corrected in #1195): stops new threads only, in-flight until idle. | `docs/product/features/ai-assistant.md` vs spec §8 | doc drift |
| **G10** | **Research gap register has no measurement.** Its revisit triggers ("users paste session ids by hand in > 10 % of prompts", "quality drift the team cannot see") are not measured by anything, so the accepted gaps (thumbs, `@` mentions, artifacts, injection classifier, multi-thread) can never be re-opened on evidence. | research §6 | process |
| **G11** | **Session actions not exposed:** rename (`PATCH /{id}`), `queue-move`, `account-pin`, `rotate`, `reload`, `suggest`, `export.md/.jsonl`, `adoptable`/`adopt`. | `api/sessions.py` | extended-flow gap |
| **G12** | **No proactive channel.** Notification channels, routing rules, quiet hours and web push all exist; the assistant can neither read them nor say "tell me when session X finishes". | `api/notifications.py`, `api/push.py`, `sessions/push.py` | v1 non-goal, natural v2 |
| **G13** | **Session-start conveniences unused.** Presets, prompt templates and permission profiles exist; `create_session` exposes only `prompt/repo/model/permission_mode/account`. | `api/presets.py`, `api/templates.py`, `api/permission_profiles.py` | convenience |
| **G14** | **No external client access.** The `ccswitch` tool table is only reachable from inside the drawer; a Claude Code Desktop/CLI user or a script cannot ask ccswitch anything. Research §3 anticipated "expose as a stdio server later". | `server.py` | integration gap |
| **G15** | **Operator actions redirect instead of act.** Deploy start, host upgrade, account refresh/enable, re-auth kick-off are excluded (A9) and the model says "that lives in …". Correct for v1; the most operator-shaped requests in an operator tool still force a context switch. | eval `cant-deploy-to-prod` | policy choice, revisit |

Not gaps (checked and correct): `get_session_transcript` filters on `textDelta`/`user`/`toolCall`,
which are the wire types the transcript endpoint emits; `list_sessions` filters assistant rows
server-side (`?kind=assistant` selects them); the `PermissionRegistry` ccswitch branch sits above
the full-auto shortcut so destructive tools prompt in every mode; `ready_merge_pr` posts to
`/api/pipeline/pr/ready-merge`, whose router prefix is indeed `/api/pipeline`.

---

## 2. Use-case taxonomy

### 2.1 What v1 was built for

| Tier | Use case | Tools that serve it | Starter |
|---|---|---|---|
| **Core** | Fleet triage — "what needs my attention?" | `list_sessions`, `get_session` | sessions |
| **Core** | Session diagnosis — "why is this stuck?" | `get_session`, `get_session_transcript` | session open |
| **Core** | Steer a session — message, interrupt, answer, allow/deny, model, mode | `send_prompt`, `interrupt_session`, `answer_question`, `resolve_permission`, `set_model`, `set_permission_mode` | session open |
| **Core** | Start work — "start a session on X to do Y" | `create_session` | sessions |
| **Core** | PR / CI diagnosis — blocked PRs, flaky checks | `list_prs`, `get_pipeline_health` | pipeline, insights |
| **Core** | Capacity awareness — quota per account, hosts online | `list_accounts`, `list_hosts` | settings |
| **Extended** | Automation visibility — "what did autopilot do?" | `list_automations`, `get_automation_events`, `set_automation_enabled`, `set_autopilot_paused` | automations |
| **Extended** | Archive management | `list_archived_sessions`, `archive_session`, `unarchive_session`, `delete_session` | archived |
| **Extended** | PR readiness | `fix_pr`, `ready_merge_pr` | pipeline |
| **Extended** | Model governance | `resolve_model_approval` | — |
| **Extended** | Queue hygiene | `cancel_queued_session` | — |
| **Extended** | Deploy awareness (read) | `get_deploy_status` | — |
| **Ancillary** | Redirect when no tool applies; refuse injected instructions; small talk | prompt policy | — |
| **Ancillary** | Page-aware suggestions and context | starters, context chip | all |

### 2.2 What v2 adds (mapped to §4 packs)

| Tier | Use case | Pack |
|---|---|---|
| **Core fix** | Chips that work; time-aware answers; pick the model; find the repo; read the failing tool output | 4.1 |
| **Extended → Core** | Product and delivery — initiatives, projects, priority queue, triage inbox | 4.2 |
| **Extended → Core** | Steer automations — accept PR-doctor proposals, dispatch backlog, act on CI/CD findings | 4.3 |
| **Extended** | Session power — rename, reorder queue, pin account, rotate, reload, export, adopt, batch | 4.4 |
| **Extended** | Start from a preset/template/profile | 4.5 |
| **Integration** | GitHub — discover repos/branches, read one PR in depth, comment, file an issue | 4.6 |
| **Integration** | "Tell me when…" — watches, notices, notification channels/rules/quiet hours | 4.7 |
| **Operator (opt-in)** | Deploy, upgrade a host, refresh/enable an account, kick off re-auth | 4.8 |
| **Integration** | Other agents and scripts drive ccswitch — stdio MCP, `ccswitch ask`; Codex-hosted assistant (spike) | 4.9 |
| **Ancillary → measured** | Thumbs, `@` mentions, capability-gap reporting, eval growth | 4.1, 4.10 |

---

## 3. Goals and non-goals (v2)

**Goals**

1. Every shipped starter chip resolves to at least one tool, and a test keeps it that way.
2. Every read the web UI performs with the user's identity has a read tool, except `settings`,
   `config`, `auth`, `hooks`, `library` mutations and credential flows, which stay excluded and are
   redirected accurately.
3. Every mutation the web UI offers has a tool **or** a documented reason it does not, in one
   table (§4.11), so the model's "that lives in …" answers are generated from the same source.
4. The assistant can be reached from outside the drawer with the same tools and the same audit.
5. The assistant can tell the user something happened without ever speaking unprompted (B2).
6. Gaps, feedback and mentions are measured, so the research gap register re-opens on evidence.

**Non-goals (unchanged from v1 unless noted)**

- Writing code, editing files, shell, arbitrary web browsing; account add/remove/credential
  changes; settings mutation (the assistant may **read** the operator-tier setting to explain
  itself, nothing else).
- Multi-thread history browser, thread sharing (B8); GenUI / live artifacts in chat; a second
  chat transport; a general-purpose GitHub client (B5).
- Auto-approval classifiers on tool output (still Phase-4-style deferred; the eval set is the
  trigger).

---

## 4. Capability packs

Each pack lists: tools (class, backing endpoint, **new HTTP** where the endpoint must be added
first — always by extracting existing WS/orchestrator logic into a shared helper, constitution VI),
allowlist rows, starters, eval cases, and its exit criterion. Common rules for every pack are in
§4.0.

### 4.0 Rules every pack obeys

- **Tool = endpoint.** A handler renames arguments, calls one route, trims, frames as untrusted.
  Logic missing from an endpoint is added to the endpoint (the UI benefits too).
- **One `ToolSpec` row, one allowlist row, one eval case minimum, one feature-doc row** per tool.
  A conformance test (`test_every_tool_hits_an_allowed_route`, plan Task 5.1) drives each handler
  against a recording fake API and asserts the route it hit is allowlisted for that method. A
  second test asserts every `TOOL_SPECS` entry appears in `cases.jsonl` `expect_tools` at least once.
- **Descriptions are onboarding docs for the model**: purpose, when to prefer a sibling, return
  shape. Return titles alongside ids (research §3).
- **Result envelope** gains `"now": "<UTC ISO-8601>"` (B6) and, for list tools, `"truncated": n`
  when the tool trimmed rows. Free text stays cut at 2000 chars; tag-like `<` stays escaped.
- **Class assignment**: read = no state change; mutation = reversible or bounded; destructive =
  irreversible, or fans out to code running unattended (deploy, dispatch, host upgrade). When in
  doubt, destructive.
- **Batch tools** (B4): the tool takes `ids: string[]` (≤ 20); the permission card renders every
  target's title (`PermissionRequest.summary` gains an optional `items: string[]`); the endpoint is
  the existing batch route; class = the single-item class, promoted to destructive for
  `permanent-delete`.

### 4.1 Pack F — Fixes and foundations (G1, G2, G3, G4, G5, G9, G10)

| Tool / change | Class | Backing | Notes |
|---|---|---|---|
| `now` in envelope | — | `wrap_untrusted` | B6. Also `truncated` for trimmed lists. |
| `list_repos(query?)` | R | `GET /api/github/repos` | name, default branch, last activity; filter client-side by substring. |
| `list_branches(repo)` | R | `GET /api/github/branches?repo=` | `CreateSessionRequest` already accepts `branch`; the tool exposes it (4.5). |
| `list_stage_models()` | R | `GET /api/pipeline/repos` | per-repo CI stage models — serves the `insights` chip "What regressed in CI recently?" together with `get_pipeline_health`. |
| `get_tool_result(id, turn_id, tool_call_id)` | R | `GET /api/sessions/{id}/transcript` (existing; handler selects the `toolResult` event by id) | output cut at 4000 chars; `toolCall` rows in `get_session_transcript` now carry `turnId`/`toolCallId` so the model can drill in. |
| `export_session(id, format="md")` | R | `GET /api/sessions/{id}/export.md` | capped at 20 kB, tail-first; the "read the whole thing" escape hatch. |
| `report_gap(capability, detail?)` | R (no side effect beyond telemetry) | none — increments `assistant_gap_reports_total{capability}` and writes `assistant.gap` audit | The prompt tells the model to call it **whenever** it answers "no tool can do that". Turns G10 into a number. |
| Model picker | UI | `POST /api/sessions/{id}/model` on the assistant's own session (browser cookie, not the token) | `AssistantHeader` gains the same `<select>` the Settings group uses (`useModels()`); 400 `MODEL_GATED` shows inline and reverts. |
| Starter coverage test | test | `starters.ts` × `cases.jsonl` | every starter string is a case with non-empty `expect_tools`; `initiatives`/`projects` chips are **temporarily replaced** by chips 4.2 can serve, then restored when 4.2 lands (plan orders 4.2 right after). |
| Kill-switch copy | docs | feature doc | match v1 §8 wording. |

Allowlist: `GET /api/github/repos`, `GET /api/github/branches`, `GET _S/export.md`.
Exit: chips → tools 100 %; `now` present in every envelope; picker switches and refuses gated
tiers; eval +8 cases.

### 4.2 Pack P — Product and delivery (G1)

Reads over the SDLC spine and the PM layer (`server/api/sdlc.py`, `server/api/pm.py`), whose
automations are propose-only and human-gated — exactly the shape a card-gated assistant fits.

| Tool | Class | Backing |
|---|---|---|
| `list_initiatives(status?)` | R | `GET /api/sdlc/initiatives` |
| `get_initiative(slug)` | R | `GET /api/sdlc/initiatives/{slug}` |
| `advance_initiative(slug)` | M | `POST /api/sdlc/initiatives/{slug}/advance` |
| `list_products()` | R | `GET /api/pm/projects` ("Products" is the nav label) |
| `get_product(slug)` | R | `GET /api/pm/projects/{slug}` |
| `get_product_deps(slug)` | R | `GET /api/pm/projects/{slug}/deps` |
| `get_priority_queue(min_status?)` | R | `GET /api/pm/queue` — "what will agents do next" |
| `list_triage()` | R | `GET /api/pm/triage` + `GET /api/sdlc/triage` (two tools or one with `source`; plan picks one tool per endpoint to keep tool = endpoint) |
| `resolve_triage(proposal_id, decision)` | M | `POST /api/pm/triage/{id}/accept` \| `/reject` — one tool, two routes selected by `decision` (same pattern as `resolve_permission`) |
| `get_onboarding_status(slug)` | R | `GET /api/pm/projects/{slug}/onboarding` |
| `request_product_sync(slug)` | M | `POST /api/pm/projects/{slug}/sync` |

Excluded: `create project`, `create initiative`, `create/delete edge`, `propose-map`,
`advance-map`, `classify` — authoring flows with multi-step UI; redirect to the Products page.
Starters restored: the four G1 chips, plus "What's at the top of the priority queue?" and "What's
waiting in the triage inbox?". Eval +10.

### 4.3 Pack A — Automations control (G7)

| Tool | Class | Backing |
|---|---|---|
| `get_automation_actionable(id)` | R | `GET /api/automations/{id}/actionable` |
| `trigger_automation_action(id, item_id, kind)` | **D** | `POST /api/automations/{id}/trigger` — fires work unattended |
| `list_automation_sessions(id)` | R | `GET /api/automations/{id}/sessions` |
| `list_backlog_candidates()` | R | `GET /api/automations/backlog-runner/candidates` |
| `get_backlog_groups()` | R | `GET /api/automations/backlog-runner/groups` |
| `dispatch_backlog(candidate_ref)` | **D** | `POST /api/automations/backlog-runner/dispatch` |
| `get_cicd_findings()` | R | `GET /api/automations/cicd-doctor/findings` |
| `get_pr_doctor_consolidation()` | R | `GET /api/automations/pr-doctor/consolidation` |
| `consolidate_prs(plan_id)` | **D** | `POST /api/automations/pr-doctor/consolidate` |
| `get_pr_doctor_job(job_id)` | R | `GET /api/automations/pr-doctor/jobs/{id}` |
| `preview_schedule(cron)` | R | `POST /api/automations/schedule/preview` (a POST that mutates nothing — the conformance test allows it via an explicit `READ_POSTS` set) |

Starters: "What is the PR doctor proposing?", "What would the backlog runner pick next?", "Any
CI/CD doctor findings I should act on?". Eval +8 incl. one injection case (a finding's text
telling the model to dispatch).

### 4.4 Pack S — Session power tools (G6, G11)

| Tool | Class | Backing |
|---|---|---|
| `rename_session(id, name)` | M | `PATCH /api/sessions/{id}` (name only; other PATCH fields stay UI-only) |
| `move_in_queue(id, position)` | M | `POST /api/sessions/{id}/queue-move` |
| `pin_account(id, account \| null)` | M | `POST /api/sessions/{id}/account-pin` |
| `rotate_session(id)` | M | `POST /api/sessions/{id}/rotate` |
| `reload_session(id)` | M | `POST /api/sessions/{id}/reload` |
| `suggest_next_prompt(id)` | R | `POST /api/sessions/{id}/suggest` (read-shaped POST; `READ_POSTS`) |
| `list_adoptable_sessions()` | R | `GET /api/sessions/adoptable` |
| `adopt_session(id)` | M | `POST /api/sessions/adopt` |
| `archive_sessions(ids[])` | M (batch) | `POST /api/sessions/batch/archive` |
| `unarchive_sessions(ids[])` | M (batch) | `POST /api/sessions/batch/unarchive` |
| `resume_sessions(ids[])` | M (batch) | `POST /api/sessions/batch/resume` |
| `rotate_sessions(ids[])` | M (batch) | `POST /api/sessions/batch/rotate` |
| `delete_sessions(ids[])` | **D** (batch) | `POST /api/sessions/batch/permanent-delete` |

The allowlist gains the five static `batch/*` rows explicitly — the UUID-anchored `_S` pattern was
designed so these could be added without ambiguity. Eval +8 incl. "archive everything that's done"
expecting exactly one `archive_sessions` call.

### 4.5 Pack C — Session-start conveniences (G13)

| Tool | Class | Backing |
|---|---|---|
| `list_presets()` | R | `GET /api/presets` |
| `list_templates(tag?)` | R | `GET /api/templates` |
| `render_template(id, vars)` | R | `POST /api/templates/{id}/render` (`READ_POSTS`) |
| `list_permission_profiles()` | R | `GET /api/permission-profiles` |
| `create_session(...)` gains `preset?`, `permission_profile?`, `branch?` | M | `POST /api/sessions` — **endpoint extended** if `CreateSessionRequest` lacks the field (plan Task 7.4 verifies against the model; the `DispatchBar` uses the same body, so the UI gains it too) |

Eval +4 ("start a session from my 'review' preset on acme/billing").

### 4.6 Pack G — GitHub (G3 remainder, B5)

| Tool | Class | Backing |
|---|---|---|
| `get_pr(repo, number)` | R | **new HTTP** `GET /api/prs/{owner}/{name}/{number}` — checks with conclusions, review state, mergeability, owning session; built from the same `server/pipeline` fetchers `GET /api/prs` uses |
| `comment_on_pr(repo, number, body)` | M | **new HTTP** `POST /api/prs/{owner}/{name}/{number}/comments` — appends "— posted by the ccswitch assistant on behalf of <identity>" |
| `create_issue(repo, title, body, labels?)` | M | **new HTTP** `POST /api/github/issues` — same trailer; uses the user's `gh` credential the repos endpoint already relies on |

Excluded: merge/close/label/review/approve. Eval +5 incl. injection (PR body asks the assistant
to comment something).

### 4.7 Pack N — Proactive notices and notifications (G12, B2)

**Notifications read/write (existing endpoints):**

| Tool | Class | Backing |
|---|---|---|
| `list_notification_channels()` | R | `GET /api/notifications/channels` (URLs redacted to host) |
| `list_notification_rules()` | R | `GET /api/notifications/rules` |
| `create_notification_rule(spec)` | M | `POST /api/notifications/rules` |
| `delete_notification_rule(id)` | M | `DELETE /api/notifications/rules/{id}` |
| `get_quiet_hours()` / `set_quiet_hours(spec)` | R / M | `GET`/`PUT /api/notifications/quiet-hours` |
| `test_notification(rule)` | R | `POST /api/notifications/test` (`READ_POSTS`; dry-run) |

**Watches (new, small):** `watch(kind, target, condition, once=true)` → M, backed by **new HTTP**
`POST /api/assistant/watches` (and `GET`/`DELETE`). A watch is a `NotificationRule` row with two
new nullable columns, `once` and `owner_session_id`, delivered through a new channel kind
`"assistant"` next to `webhook`/`push`. When the existing `notify()` call site matches a rule whose
channel kind is `assistant`, it (a) sends web push as today and (b) appends an
`assistant.notice` **event** to the owner's assistant thread — rendered by `TranscriptView` as a
system-styled notice card ("Session *Refactor auth* finished · 2 min ago") with an **Ask about it**
chip that pre-fills the composer. No model turn is started (B2). Conditions in v2: session status
enters a set; PR checks conclude; account becomes limited; automation event of a kind. Cap
`CCSWITCH_ASSISTANT_WATCH_MAX_PER_IDENTITY` (20).

Starters: "Tell me when this session finishes", "Notify me if any account gets limited". Eval +6.

### 4.8 Pack O — Operator tier (G15, B1; amends v1 A9)

Registered **only** when `AppSettings.assistant_operator_tools` (→ `CCSWITCH_ASSISTANT_OPERATOR_TOOLS`,
default false) is on at thread creation; the allowlist rows are conditional on the same predicate,
checked per request. All class **D** except where noted; never always-allowed; audit
`via="assistant"` plus `detail.operatorTier=true`.

| Tool | Class | Backing |
|---|---|---|
| `start_deploy(ref?)` | D | `POST /api/deploy/start` |
| `upgrade_host(name)` | D | `POST /api/fleet/hosts/{name}/upgrade` |
| `refresh_account(name)` | M | `POST /api/accounts/{name}/refresh` |
| `set_account_enabled(name, enabled)` | M | `PATCH /api/accounts/{name}/enabled` |
| `start_reauth(name)` | M | `POST /api/accounts/{name}/reauth` — returns the flow URL; the human completes it in the browser |

Still excluded, permanently: add/remove accounts, `reauth/code`, host add/remove, settings PUT,
hooks, library/snapshot mutations. The prompt gains one sentence: "Operator tools are enabled in
this deployment" / "…are disabled; say so and point to Settings → General → AI assistant".
Settings gains the switch with a warning line. Eval +6 in both states.

### 4.9 Pack X — External access and engine neutrality (G8, G14, B3, B7)

**User-scoped token `ccas2:`** — `POST /api/assistant/tokens {scope: "read"|"act", ttl_days ≤ 30}`
(browser identity only, never via the assistant) mints a signed token whose hash is stored in a new
table `assistant_tokens` (id, identity, scope, created_at, expires_at, revoked_at, last_used_at);
`GET`/`DELETE` list and revoke. `IdentityMiddleware` accepts `ccas2:` from **any** peer but only
over the existing tailnet/passkey-gated listener, sets `via="mcp-external"`, and applies the same
`ASSISTANT_ROUTE_ALLOW` (scope `read` additionally drops every non-GET row). New table → the
Postgres compat lists (`_REQUIRED_EXTRAS`, `RECONCILE_EXEMPT`) gain entries (CLAUDE.md rule).

**`ccswitch mcp`** (new CLI entry point in `src/ccswitch/`): a stdio MCP server that instantiates
`AssistantApi(base_url=<edge>, token=<ccas2>)` and serves `build_tools(api)` over the official
`mcp` Python SDK — the same tool objects the drawer uses, so behaviour and audit are identical.
Documented `.mcp.json` snippet for Claude Code Desktop/CLI. Operator-tier tools are never served
externally regardless of the setting.

**`ccswitch ask "<prompt>" [--json]`**: opens (or resumes) the caller's assistant thread through
`POST /api/assistant/session`, sends one prompt over the WS with `context={route:"/cli"}` (route
regex extended), streams the answer to stdout, exits after `turnEnd`. Mutations still raise cards
— in the CLI a card is printed and answered with `--yes` / interactive prompt / denied on
`--no-input`. This is the hook for cron, Slack bridges and other systems.

**Engine spike** (constitution III, its own task): can `server/codex/driver.py` mount an in-process
MCP server and honour a tool allow-only list? Findings doc decides between (a) moving
`assistant_options` behind `engine_policy` so an assistant thread can run on a Codex account, and
(b) `GET /api/assistant/status` returning `engineUnavailable: true` with the drawer explaining
"the assistant needs a Claude account". Either outcome closes G8.

### 4.10 Pack U — UX and feedback loop (G10, research §6)

| Change | Where | Measures |
|---|---|---|
| Thumbs up/down per assistant turn | `TranscriptView variant="assistant"` turn footer → `POST /api/assistant/feedback {turnId, vote, note?}` → `assistant.feedback` audit + `assistant_feedback_total{vote}` | research gap "thumbs"; "quality drift the team cannot see" |
| `@` mentions in the composer | typeahead over `useSessions()` titles → inserts `[title](/sessions/<id>)`; increments `assistant_mentions_total` | research trigger "> 10 % paste ids by hand" becomes `mentions / prompts` |
| Suggested follow-up chips after an answer | reuse the existing `suggestions` event the coding driver already emits (idle suggestions); the assistant kind enables it with an assistant-specific prompt | adoption (prompts / active day) |
| Eval growth | cases ≥ 80; per-pack fixtures; a `--pack` filter; nightly `evals.yml` job posts pass rate as a metric | ≥ 90 % gate |
| Gap dashboard | `assistant_gap_reports_total` by capability in the existing Insights page (one table) | closes the loop on §0.2(c) |

### 4.11 Exclusion table (single source for "that lives in …")

Generated into the system prompt **at build time** (a test asserts the prompt text matches this
table, so the model's redirects cannot drift from policy):

| Surface | Why excluded | Redirect |
|---|---|---|
| Add / remove account, credentials, `reauth/code` | credential authority (constitution II/IV) | Settings → Accounts |
| Settings PUT, `/api/config` | global blast radius; contains operator policy | Settings |
| Hooks | arbitrary shell on the host | Settings → Hooks |
| Library mutations (plugins/skills/commands/MCP servers), snapshots restore/import | shared config base; rollback semantics live in the UI | Library page |
| Fleet host add / remove | topology change | Settings → Fleet |
| Project/initiative authoring, feature-map proposals | multi-step reviewed flows | Products / Initiatives pages |
| PR merge/close/label/review beyond `ready_merge_pr` | B5 | the coding session or GitHub |
| Deploy / upgrade / account refresh **when operator tier is off** | B1 | Settings → General → AI assistant |

---

## 5. Data model

All columns nullable (reconcile applies them); one new table (Pack X) with the compat-list rule.

| Table | Column | Pack |
|---|---|---|
| `app_settings` | `assistant_operator_tools: bool NULL` | O |
| `notification_channels` | `kind` gains value `"assistant"` (no schema change) | N |
| `notification_rules` | `once: bool NULL`, `owner_session_id: str NULL`, `expires_at: datetime NULL` | N |
| `assistant_tokens` (**new**) | `id, identity, scope, token_hash, created_at, expires_at, revoked_at NULL, last_used_at NULL` | X |
| `audit_events` | none — `via` values gain `"mcp-external"`, `"cli"` | X |

Alembic revisions `0024_assistant_watches`, `0025_assistant_tokens` chained off the current head.

---

## 6. Events and wire additions

- `assistant.notice` event (`type: "notice"`, `{kind, title, body, url, at, watchId}`) — rendered
  as a notice card; replayable; never a model turn.
- `PermissionRequest.items?: string[]` for batch cards.
- `toolCall` rows in `get_session_transcript` output carry `turnId`, `toolCallId`.
- `GET /api/assistant/status` gains `operatorTools: bool`, `engineUnavailable?: true`.

---

## 7. Configuration

| Setting | Env | `AppSettings` | Default |
|---|---|---|---|
| Operator tier | `CCSWITCH_ASSISTANT_OPERATOR_TOOLS` | `assistant_operator_tools` | false |
| External tokens allowed | `CCSWITCH_ASSISTANT_EXTERNAL_TOKENS` | — | false |
| Watches per identity | `CCSWITCH_ASSISTANT_WATCH_MAX_PER_IDENTITY` | — | 20 |
| External token max TTL | `CCSWITCH_ASSISTANT_TOKEN_MAX_DAYS` | — | 30 |

---

## 8. Security review (constitution IV) — additions over v1 §8

| Threat | Mitigation |
|---|---|
| Operator tools fire from an injected instruction | class D → card in every mode, never always-allowed; not registered when off; audit `operatorTier` |
| Batch tool mis-scoped by the model | card enumerates every title; cap 20; destructive class for delete |
| External token leaks | `ccas2:` only accepted behind the tailnet/passkey gate; scope `read` default; hash-stored, revocable, TTL ≤ 30 d; `last_used_at` for review; operator tools never served externally |
| `ccswitch ask` in a script approves its own cards | `--yes` is explicit; `--no-input` denies; every answer is audited `via="cli"` |
| Watch spam / notice flood | per-identity cap; `once` default; quiet hours honoured by the same engine |
| GitHub writes attributed to the human | trailer names the assistant and the identity; content is the model's — the card shows the full body before posting |
| Prompt redirects drift from policy | §4.11 table is the generator and the test |
| Tool count grows past what the model handles well | ~75 tools after v2; descriptions stay one-purpose; the eval gate (≥ 90 %) is the tripwire; if it trips, Phase 10 adds tool-search (`ENABLE_TOOL_SEARCH`) rather than pruning |

---

## 9. Testing strategy

- Conformance: every tool → allowlisted route (Task 5.1); every tool → ≥ 1 eval case; every
  starter → ≥ 1 eval case; prompt exclusion text == §4.11 table.
- Unit per pack: handlers with `httpx.MockTransport`; batch card `items`; watch rule → notice
  event; `ccas2:` mint/verify/revoke/scope; operator predicate on registration and allowlist.
- API: new HTTP endpoints (PR detail, PR comment, issue create, watches, tokens, feedback) with
  `via` handling.
- Web: model picker (incl. gated 400), notice card + "Ask about it", thumbs, `@` typeahead,
  Settings operator switch.
- Live smoke additions: one operator-tier card with the setting on; `ccswitch ask "list my
  sessions"` end-to-end; `ccswitch mcp` listing tools from a real MCP client.
- Eval: grows from 40 to ≥ 80; nightly run; pass rate as a metric.

---

## 10. Risks and open questions

| Risk | Mitigation |
|---|---|
| Tool sprawl degrades tool selection | eval gate; tool-search fallback (§8 last row) |
| `CreateSessionRequest` lacks preset/profile/branch | Task 7.4 verifies first; extending the endpoint is in scope (rule: add to the endpoint) |
| Codex cannot mount in-process MCP | spike decides (B7); status flag path is cheap |
| Notice cards make the thread noisy | notices collapse after 24 h; `once` default; cap |
| External token becomes a general API key by drift | route allowlist is shared with `ccas1:`; scope check is in the middleware, not the tools |

**Open for the reviewer** (defaults applied in the plan): v1 §10 Q1–Q3 remain; plus **Q4** should
operator tier default **on** for single-operator deployments (chosen: off); **Q5** should
`ccswitch ask` be able to answer cards at all (chosen: yes, explicit flags only).

---

## 11. Success metrics (30 days after Phase 10)

| Metric | Target |
|---|---|
| Starter chips backed by a tool | 100 % (test-enforced) |
| Eval pass rate at ≥ 80 cases | ≥ 90 % |
| `assistant_gap_reports_total` / prompts | < 5 % and falling |
| Mutation cards denied | < 10 % (unchanged) |
| Thumbs-down / turns | < 10 % |
| `mentions / prompts` | reported (decides the research trigger) |
| External calls (`via=mcp-external`+`cli`) / week | > 0 (adoption of Pack X) |
| Watches created / week | > 0 (adoption of Pack N) |
