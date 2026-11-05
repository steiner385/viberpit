# Release Milestones Phase 3 (Agent Access) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sessions read and update milestones through the `viberpit` MCP server, so "update the roadmap" becomes a tool call. The AI assistant drawer can read milestones too.

**Architecture:** There are four session tools. All of them reach the Phase 2 HTTP handlers in-process, so every write rule stays in `server/pm/milestone_actions.py` and nothing is copied.

| Tool | Class | Path | Notes |
|---|---|---|---|
| `roadmap_milestones` | READ | `session_triage.invoke` | Reads the milestone view. |
| `roadmap_milestone_tick` | MUTATION, card | `session_triage.invoke` | Uses the human owner's identity, `via="session"`. |
| `roadmap_milestone_advance` | MUTATION, card, never-ruled | `session_triage.invoke` | Uses the human owner's identity, `via="session"`. |
| `roadmap_milestone_note` | SIGNAL, no card (M8) | Own small handler | Acts as the session itself (`session:<id>`), so it works in unattended sessions. |

The assistant gets one read tool, `get_milestones`, which is one loopback GET.

**Spec:** `docs/superpowers/specs/2026-11-05-release-milestones-design.md`, sections M4, M8, M10, M11 and §11. Phases 1 and 2 are #3316 and #3323.

## Rulings carried in this plan

- **R1 (spec M8, made real).** Phase 2 required a person for every write. M8 says notes, the status line and refresh work from any session with no card, and the session is the recorded actor.
  - `add_note`, `update_note` and `set_status_note` also accept a **session actor**, meaning an identity with the `holds.SESSION_PREFIX` (`session:`).
  - A session may change only notes it created. Changing anything else stays owner-only, unchanged.
  - Ticks and advance stay person-only. They go through the card path, where the identity is the session's human owner and `via="session"`.
- **R2.** `roadmap_milestone_note` is `ToolClass.SIGNAL`. The permission gate has no "no-card mutation" class, and `propose_decision` is the precedent for a SIGNAL roadmap write by a session. It is not a turn-ending signal.
- **R3.** `roadmap_milestone_advance` has no override parameter. An override is owner-only through the web UI (M4/M10). The tool is added to `NEVER_RULED_TOOLS`, so "always allow" can never cover it, just like `roadmap_advance`.

## Global Constraints

- One implementation per concept. Tool handlers call the Phase 2 route handlers in-process, as `session_triage` does for `roadmap_advance`, and never re-implement rules.
- Pydantic tool arguments use `extra="forbid", strict=True` (inherit `session_triage.Arguments`). A tool can never name an identity.
- Every write is audited. Session-path mutations also write `pm.session.delegate`.
- No `ccswitch` spellings. The tool names use the `viberpit` brand prefixes through `MCP_PREFIX`.
- Server gate: four steps. Web gate: typecheck, lint, test, build and budget. The ceiling is 307.9 and the build is at 307.6, so the only web change is the assistant action title.

## Review Focus

1. **An unattended session calls `roadmap_milestone_note` to resolve an ask the owner created.** Expected: 403 `owner_required`, surfaced as a tool error naming the reason. Pinned in Task 1.
2. **A `bypassPermissions` session calls `roadmap_milestone_advance`.** Expected: refused by `_owner`, the same as `roadmap_advance`. Pinned in Task 2.
3. **`roadmap_milestones` on a product with no ladder.** Expected: a short "no ladder: add docs/product/milestones.yaml" text, not an error. Pinned in Task 2.
4. **`roadmap_milestone_tick` on a computed condition.** Expected: a tool error carrying the `not_manual` copy. Pinned in Task 2.
5. **The assistant tries a milestone write route.** Expected: denied by `route_allowed`, because only GET is allowlisted. Pinned in Task 3.

---

### Task 1: Session actors for notes and the status line (R1)

**Files:** Modify `server/pm/milestone_actions.py`. Test: `server/tests/test_pm_milestone_actions.py` (extend).

**Interfaces:**
- Add `is_session(actor) -> bool`, true when `actor.identity` starts with `holds.SESSION_PREFIX`.
- Add `_require_writer(actor)`, which allows `is_person(actor) or is_session(actor)` and otherwise raises 403 `person_or_session_required`. It replaces `_require_person` in `add_note`, `update_note` and `set_status_note` only.
- Ownership rule in `update_note`: when `note.created_by` differs from `actor.identity`, `is_owner(actor)` is still required. A session can therefore edit or resolve only its own notes.
- `set_status_note` from a session is allowed.
- `tick_condition` and `advance` stay `_require_person`.

- [ ] **Step 1: Write the failing tests.**
  - `test_session_actor_can_add_note`: `created_by == "session:abc"`.
  - `test_session_actor_can_set_status_note`
  - `test_session_can_resolve_own_ask`
  - `test_session_cannot_resolve_owners_ask` (403 `owner_required`)
  - `test_session_actor_cannot_tick` (403 `person_required`)
  - `test_session_actor_cannot_advance`
  - `test_system_actor_cannot_add_note` (`automation:x` → 403 `person_or_session_required`)
- [ ] **Step 2–4:** confirm the tests FAIL, implement, then confirm they PASS. Add `person_or_session_required` to the web `MILESTONE_ERROR_COPY` with a test. **Commit:** `feat(pm): sessions may write milestone notes and status lines as themselves (M8)`

### Task 2: Session MCP tools

**Files:**
- Modify:
  - `server/sessions/tools/registry.py`: specs, the `mounted_signal_names` entry for the note tool, and the `mounted_names` mutation tuple for tick and advance.
  - `server/sessions/tools/roadmap.py`: the handlers.
  - `server/pm/session_triage.py`: the models and dispatch for read, tick and advance.
  - `server/sessions/tools/instruction.py`: the `_BLURBS`.
  - `server/permission_rules/patterns.py`: `NEVER_RULED_TOOLS` gets `roadmap_milestone_advance` under both prefixes.
  - `docs/superpowers/evals/session-tools/cases.jsonl`
  - `docs/product/features/session-tools.md`
- Create: `server/sessions/tools/milestone_note.py`, the SIGNAL handler.
- Test:
  - Create `server/tests/test_session_milestone_tools.py`.
  - Update the exact-set tests: `test_session_tools.py`, `test_codex_session_tools.py`, `test_session_signal_tools.py`, `test_session_tools_instruction.py` and `test_session_roadmap_permissions.py`. Grep for `NEVER_RULED_TOOLS` and `roadmap_advance` in server/tests and update the pattern tests as well.

**Interfaces:**
- Specs:
  - `ToolSpec("roadmap_milestones", READ, "Read product milestones")`
  - `ToolSpec("roadmap_milestone_tick", MUTATION, "Tick a manual milestone condition", idempotent=False)`
  - `ToolSpec("roadmap_milestone_advance", MUTATION, "Advance the current milestone", idempotent=False)`
  - `ToolSpec("roadmap_milestone_note", SIGNAL, "Record a milestone note", idempotent=False)`
- Argument models (in session_triage, strict):
  - `MilestonesRead(product: str)`
  - `MilestoneTick(product: str, milestone: str, condition: str, met: bool | None = None, value: float | None = None, evidence: str, url: str | None = None)`
  - `MilestoneAdvance(product: str, expected_current: str | None)`
- Add `MilestoneTick` and `MilestoneAdvance` to the `mutation` tuple. Each calls the Phase 2 route handler in-process (`pm_milestones.<handler>(slug, Body(...), request, identity=identity, db=db)`). Read the exact function names in `server/api/pm_milestones.py`.
- `MilestoneNoteArgs` (strict) has `product: str`, `action: Literal["add", "set_status", "status_line", "refresh"]`, and the optional fields `kind`, `text`, `ref`, `url`, `milestone`, `note_id` and `status`.
  - The handler builds `Request` with `state.identity = f"{SESSION_PREFIX}{ctx.session_id}"` and `state.via = "session"`.
  - It calls the Phase 2 handlers in-process inside `db_session()`. `add` maps to the post-note handler, `set_status` to the patch-note handler, `status_line` to the patch-milestone handler (`milestone` is required), and `refresh` to the refresh route handler.
  - It writes `pm.session.delegate` with `{sessionId, tool, arguments}` in the same transaction.
  - It maps `HTTPException` and `ValidationError` to `ToolArgError` with the server's error code in the message.
  - It does not call `_owner`. That is the point of R1 and R2, and unattended sessions work.
- Tool output for all four is `{"text": <compact summary>, "data": <view JSON>}`. Write the summary as `summarize_milestones(view) -> str` in a new pure function in `server/pm/milestone_view.py`, so it has a single definition:

```
Launch readiness — current: Phase 0 · Household gate (1/4 met, 1 unknown, ready: no)
  ✔ consent: Card-consent charge captured… (3 of 3 closed, 11m ago)
  ✖ lane-fixes: Nine lane-integrity fixes landed (8 of 9 closed, 11m ago)
  ✖ nightly: Nightly full suite green… (0 of ≥3, 13h ago)
  ? rubric: Evidence rubric satisfied… (unknown: no viberpit-metrics artifact…)
  leaves: 17 done · 0 in flight · 1 not started
Open asks (2): [community] Post sign-off on #20271 · [friendly] Fill phase1_households
Decisions: D1 locked, D2 locked, … (8)
Upcoming: Phase 1 · Friendly families, Phase 2 · Community cohort, Phase 3 · Public self-serve
```

  - For a product with no ladder, the text is `"<product>: no milestone ladder (add docs/product/milestones.yaml)"` and `data` is null.
  - Keep the summary under about 1,500 characters. Truncate condition labels to 60 characters, and when a list runs long, end it with "+N more".
- Blurbs: one line per tool, saying when to use it. Example for the note tool: "Record an owner decision or ask, update a note you created, set a milestone's status line, or refresh measurements — no approval card."
- Eval cases: one per tool, using these prompts:

| Tool | Prompt | `expect_tools` |
|---|---|---|
| `roadmap_milestones` | "Which launch phase are we in and what's left in its gate?" | `["roadmap_milestones"]` |
| `roadmap_milestone_tick` | "The owner confirms every family finished setup without a support call; tick it with that evidence." | `["roadmap_milestones", "roadmap_milestone_tick"]` |
| `roadmap_milestone_advance` | "The household gate is green — move us to the next phase." | `["roadmap_milestones", "roadmap_milestone_advance"]` |
| `roadmap_milestone_note` | "Record that the owner needs to fill in phase1_households." | `["roadmap_milestone_note"]` |

  The forbid lists and rubrics follow the existing roadmap cases.
- Doc: add a "Release milestones" subsection to `docs/product/features/session-tools.md` next to "Roadmap triage", covering the four tools, their classes and R1–R3, and update the tool count line.

- [ ] **Step 1: Write the failing tests** in `test_session_milestone_tools.py`, using the existing session-roadmap test harness (grep `test_session_roadmap.py` for how it builds `ctx` and calls a tool):
  - `test_read_returns_summary_and_view`
  - `test_read_no_ladder_text`
  - `test_tick_calls_rules_with_owner_identity_and_session_via`: the audit has `via="session"`, and `manual_by` is the person.
  - `test_tick_computed_condition_is_tool_error_not_manual`
  - `test_advance_when_ready`
  - `test_advance_gate_unmet_is_tool_error`
  - `test_advance_refused_in_bypass_permissions`
  - `test_note_add_records_session_actor`: `created_by` is `session:<id>`, and the delegate audit is present.
  - `test_note_works_without_human_owner` (unattended ctx)
  - `test_note_cannot_resolve_owner_ask`
  - `test_note_status_line_and_refresh`
  - `test_advance_is_never_ruled`
  - `test_note_is_signal_no_card`: `PermissionRegistry.request` resolves without pending.
  - `test_tick_and_advance_card`
  - `test_summarize_milestones_format`, against the fixture view.
  - Also update the exact-set tests.
- [ ] **Step 2–4:** confirm the tests FAIL, implement, then confirm they PASS. Then run `pytest server/tests -k "session_tool or session_roadmap or codex_session or signal_tools or permission or mcp_spec or milestone" -n 4` in the foreground, plus ruff and `mypy server/`. **Commit:** `feat(sessions): roadmap_milestone* session tools`

### Task 3: Assistant read tool `get_milestones`

**Files:**
- Modify:
  - `server/assistant/tools.py`: the spec, the handler and `base_tools`.
  - `server/assistant/policy.py`: the allowlist row `("GET", r"/api/pm/projects/[a-z0-9-]+/milestones")`, GET only.
  - `docs/superpowers/evals/assistant/cases.jsonl`: one case.
  - `docs/product/features/ai-assistant.md`: tool counts and the pack table row (add the tool to the P — product and delivery row).
  - `web/src/session/appActions.ts` with `__fixtures__/appActionToolKeys.json`, setting `get_milestones: 'Read milestones'`.
- Test:
  - `server/tests/test_assistant_conformance.py`: `ARGS` entry `{"slug": "a"}`.
  - `server/tests/test_assistant_tools.py`: the request shape.
  - The policy test `test_milestone_write_routes_not_allowlisted`, covering PUT, POST and PATCH on the milestone routes and the refresh POST.

**Interfaces:**
- `get_milestones(slug)` does `GET /api/pm/projects/{slug}/milestones` over loopback, through `_slug`.

- [ ] **Step 1: Write the failing tests:** the conformance entry, the request shape, the allowlist negative test, and the eval coverage. **Step 2–4:** confirm they FAIL, implement, then confirm they PASS. Run `pytest server/tests -k assistant -n 4`, ruff, `mypy server/`, and the full web gate (the budget must pass with no ceiling change). **Commit:** `feat(assistant): get_milestones read tool`
