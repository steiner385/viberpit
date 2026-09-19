# Hold Ladder — 5: decision proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A coding session that hits a genuine product decision proposes options instead of guessing or handing back; the person picks one in the inbox; the next implement run carries the accepted decision verbatim.

**Architecture:** One new table `decision_proposals` (proposal + options + recommendation + rationale + chosen key + status), written together with a `decision` hold by `session:<id>` in one transaction by ONE service function that both the HTTP route and the `propose_decision` session tool call. Accept/reject are two routes on the same router; accept clears the hold (the only path that may), records the chosen key, comments on the issue; reject writes the person's owner-action hold. The runner treats an ended session whose resolution is `awaiting-decision` as neither a bounce nor a hand-off (cause `decision_proposed`), and the spawn threads accepted decisions for the batch's issues into the implement prompt through a new `Batch` field. The inbox lists decision proposals beside classification proposals from one widened endpoint, with a card whose options are a single-choice group. The explainer gains `decision_guidance`; the assistant gains `resolve_decision`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async · alembic revision as the record (reconcile applies) · React+Vite+TS · pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` §6 (decision proposals), §7 (dialog: Decision links to the proposal card), §8 (provenance: decision holds only via the decisions endpoint; `propose_decision` tool; a `False` regression row per new mutating route in `test_assistant_policy.py`), decision 3 (draft options, wait for accept — no veto window).

## Global Constraints

- Constitution VI: ONE proposal writer (`server/pm/decisions.py:propose`), ONE accept (`decisions.accept`), ONE reject (`decisions.reject`), each used by the route AND any tool; the session tool follows the existing in-process handler pattern of `set_hold` (`server/sessions/tools/tools.py`) — it calls the same service function the route calls, never a second implementation; the assistant tool is one loopback HTTP call (`server/assistant/tools.py` pattern). No second "pending" definition on the web: `triageSelectors.pendingProposals` stays the one predicate and learns the new kind.
- Holds only through `server/pm/holds.py`; a `decision` hold is set only by `decisions.propose` (actor `session:<id>` or a person) and cleared only by `decisions.accept` (`release_via_api` already refuses it generically — keep that). Reject writes `owner_action` by the person with reason `"decision proposal rejected"` via `set_reject_hold`'s sibling (add `DECISION_REJECT_REASON`, one constant).
- New table → migration `0053_decision_proposals.py` chained off the newest revision on master AT COMMIT TIME (re-check `ls server/db/migrations/versions/ | sort | tail -1`; #1964 adds `0052_pr_resolution_note` — if it has not merged, chain off 0051 and the controller renumbers at rebase). Add the table to BOTH hand-maintained lists: `_REQUIRED_EXTRAS` in `server/tests/test_postgres_compat.py` and, for any NOT NULL column without a synthesizable default, `RECONCILE_EXEMPT` in `server/tests/test_schema_reconcile_coverage.py`. Prefer nullable columns with defaults.
- Audit: `pm.decision.propose {workItemId, proposalId, actorClass}`, `pm.hold.set` (kind decision), `pm.decision.accept {proposalId, chosenKey}`, `pm.hold.release` (signal `decision`), `pm.decision.reject`, `pm.hold.set` (owner_action). `RELEASE_SIGNALS` gains `"decision"`.
- `NO_PR_RESOLUTIONS` gains `"awaiting-decision"` (hyphenated like the others); the runner maps it to outcome `no_pr` cause `decision_proposed` and neither climbs nor hands off.
- No absolute date literals in tests; no new `CCSWITCH_` literals (ratchet ceiling exact); commit trailer literally `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; messages via file + `git commit -F`; keep deploy/release/rollout/promote and prod/production apart in any command text (NB: "product" contains "prod" — write doc text with Write/Edit, run gh/git bare).
- Test invocation: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …` (foreground, never the whole `server/tests/`). Web gates: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`.
- Branch `hold-ladder-5` off origin/master 9efa79dd (independent of 4b; rebase before the PR).

## File structure

| File | Responsibility |
|---|---|
| `server/db/models.py` | `DecisionProposal` model; `DECISION_PROPOSAL_STATUSES = ("pending","accepted","rejected")` |
| `server/db/migrations/versions/0053_decision_proposals.py` | the record |
| `server/pm/decisions.py` (new) | `propose(db, wi, *, options, recommended_key, rationale, actor, now)`, `accept(db, proposal, *, chosen_key, actor, now)`, `reject(db, proposal, *, actor, now)`, `pending_for_refs(db, refs)`/`accepted_text_for_refs(db, refs)`, validation (2–6 options, unique keys, recommended in keys), `DecisionError` |
| `server/pm/holds.py` | `DECISION_REJECT_REASON`, `RELEASE_SIGNALS + "decision"`, `set_decision_hold`, `clear_decision_hold` (the only clearer) |
| `server/api/pm.py` | `POST /pm/work-items/{id}/decisions`, `GET /pm/decisions` (pending list), `POST /pm/decisions/{pid}/accept`, `POST /pm/decisions/{pid}/reject`; GitHub comment on accept via the existing announce helper family (`server/automations/backlog_github.py`) |
| `server/pm/explain.py` | `decision_guidance(*, policy)` → `{"accept": …, "reject": …}` |
| `server/sessions/completion.py` | `NO_PR_RESOLUTIONS + "awaiting-decision"` |
| `server/sessions/tools/{registry,tools,instruction}.py` | `propose_decision` SIGNAL tool (+ eval case in `docs/superpowers/evals/session-tools/cases.jsonl`) |
| `server/automations/backlog_runner.py` | no-PR branch: resolution `awaiting-decision` → cause `decision_proposed`, release, no ladder/hand-off |
| `server/automations/backlog_selection.py`, `backlog_grouping.py`, `backlog_prompt.py`, orchestrator `_backlog_spawn` | `Batch.decisions: dict[str, str]` (ref → accepted decision text), threaded verbatim into the implement prompt |
| `server/assistant/tools.py`, `server/assistant/policy.py` | `resolve_decision` tool + allowlist rows; `test_assistant_policy.py` `False` rows |
| `web/src/api/{triage,pm}.ts`, `hooks.ts`, `features/roadmap/inbox/*`, `IssueDispositionDialog.tsx` | decision proposals in the inbox: card with a radio group, Accept/Reject, guidance; dialog link for a decision hold |
| docs | runner doc, PM-layer doc, session-tools doc, ai-assistant doc, spec §14 |

---

### Task 1: The table, the service, the hold rules

**Files:** Modify `server/db/models.py`, `server/pm/holds.py`; Create `server/pm/decisions.py`, `server/db/migrations/versions/0053_decision_proposals.py`; Tests: `server/tests/test_pm_decisions.py` (new), `server/tests/test_decision_proposals_migration.py` (new, copy the shape of `test_work_item_holds_migration.py`), `server/tests/test_pm_holds.py`, `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`), `server/tests/test_schema_reconcile_coverage.py` if needed.

**Interfaces:**
- `DecisionProposal` (table `decision_proposals`): `id` (str uuid pk), `work_item_id` (fk work_items.id, indexed), `session_id` (str | None), `options` (JSON list of `{"key": str, "label": str, "detail": str | None}`), `recommended_key` (str), `rationale` (str), `chosen_key` (str | None), `status` (str, default "pending"; `DECISION_PROPOSAL_STATUSES`), `proposed_by` (str actor), `resolved_by` (str | None), `created_at` (naive UTC via `db_utc_now()` default — never `func.now()`), `resolved_at` (naive UTC | None). All nullable except id/work_item_id/status (status has a literal default the reconciler can synthesize; if `_ddl_default_literal` cannot, make it nullable and default in Python).
- `decisions.propose(db, wi, *, options, recommended_key, rationale, actor, now) -> DecisionProposal`: validates (2 ≤ len(options) ≤ 6, keys unique non-empty, `recommended_key in keys`, rationale non-empty), refuses when the item already has a pending proposal (`DecisionError("a decision is already pending")`) or when `wi.hold_kind in HELD_KINDS and wi.hold_by` is a person (a person's park wins), writes the row AND `holds.set_decision_hold(wi, reason=f"decision pending: {rationale[:120]}", actor=actor, now=now)` — same `db` session, caller commits; audits `pm.decision.propose` and `pm.hold.set`.
- `decisions.accept(db, proposal, *, chosen_key, actor, now) -> DecisionProposal`: `chosen_key in keys` else `DecisionError`; status → accepted, `chosen_key`, `resolved_by/at`; `holds.clear_decision_hold(wi, actor=actor)` + `pm.hold.release` audit signal `"decision"`; `pm.decision.accept` audit `{proposalId, chosenKey}`.
- `decisions.reject(db, proposal, *, actor, now)`: status → rejected; `holds.set_reject_decision_hold(wi, actor=actor, now=now)` (owner_action, reason `DECISION_REJECT_REASON = "decision proposal rejected"`, `hold_by = actor`); audits.
- `decisions.accepted_text_for_refs(db, refs) -> dict[str, str]`: for each ref with an accepted proposal (newest), the verbatim text `f"Decision ({label}): {detail or label} — {rationale}"` — ONE formatter used by the prompt.
- `decisions.pending_list(db) -> list[DecisionProposal]` joined to work items (repo/number/title/product) for the inbox.
- `holds.set_decision_hold` / `clear_decision_hold` (the only clearer; `release_via_api` keeps refusing `decision`); `RELEASE_SIGNALS += ("decision",)` with a comment; update the pinned set assertion in `test_pm_holds.py`.

- [ ] Step 1 tests (write them first): propose writes row + hold + two audits in one flush; a second propose on the same item raises; propose on an item parked by a person raises; accept with a bad key raises; accept clears the hold (signal decision) and records the key; reject writes the owner_action hold with the constant reason and the person as `hold_by`; `accepted_text_for_refs` formats verbatim; migration guarded twice; chain linear (`-k exactly_one_head`); `_REQUIRED_EXTRAS` updated so `test_postgres_compat.py` collects (it skips without `CCSWITCH_TEST_PG_URL` — just keep the list complete).
- [ ] Step 2 run → fail. Step 3 implement. Step 4 run the files + `server/tests/test_schema_reconcile_coverage.py server/tests/test_pm_holds.py -q`, ruff, mypy on the new modules, brand ratchet. Step 5 commit `feat(pm): decision proposals — table, service, and the decision hold rules`.

---

### Task 2: The API routes and the explainer

**Files:** Modify `server/api/pm.py`, `server/pm/explain.py`, `server/automations/backlog_github.py` (an `announce_decision` sibling of `announce_needs_human` — one comment with the chosen option); Tests: `server/tests/test_pm_decisions_api.py` (new; copy the auth/actor fixture pattern of the hold-release route tests in `test_pm_api.py`), `server/tests/test_pm_explain.py`.

**Interfaces:**
- `POST /api/pm/work-items/{id}/decisions` body `{options, recommended_key, rationale}` → 201 with the proposal; actor via `actor_for_request` (person, session:<id>, assistant, lan@local — all allowed to propose; a `decision` hold is the one hold class every actor may set THROUGH THIS ROUTE ONLY); 409 on a pending proposal; 422 on validation.
- `GET /api/pm/decisions` → pending proposals with `ref`, `title`, `product`, `options`, `recommendedKey`, `rationale`, `proposedBy`, `createdAt`, `guidance: decision_guidance(policy=<product policy>)`.
- `POST /api/pm/decisions/{pid}/accept` body `{chosen_key}` → clears the hold, records, posts the GitHub comment best-effort ("Decision accepted: <label> — <detail>. Chosen by <actor>."), returns the proposal; 409 if not pending.
- `POST /api/pm/decisions/{pid}/reject` → owner_action hold by the actor; returns the proposal.
- `decision_guidance(*, policy) -> {"accept": ConsequenceWire, "reject": ConsequenceWire}`: accept → "the hold clears; the next run implements with the chosen option" (`then` step, `spawnsSession` per the product's dispatch policy like `triage_guidance`); reject → "the issue is held for you with 'decision proposal rejected'; Retry or Implement now release it". Reuse `triage_guidance`'s helpers; no second consequence builder.
- Wire keys camelCase like the triage list.

- [ ] Step 1 tests: each route's happy path + 409/422; the accept path calls the announce seam once (monkeypatch); `decision_guidance` shape. Step 2 fail. Step 3 implement. Step 4 run + ruff + mypy + ratchet. Step 5 commit `feat(pm): decision proposal routes and consequence guidance`.

---

### Task 3: The session tool, the resolution, the runner

**Files:** Modify `server/sessions/tools/registry.py`, `tools.py`, `instruction.py`, `server/sessions/completion.py`, `server/automations/backlog_runner.py`, `docs/superpowers/evals/session-tools/cases.jsonl`; Tests: `server/tests/test_session_tools.py`, `test_session_tools_instruction.py`, `test_completion.py`, `test_backlog_runner_loop.py`, `test_translator_signal_tools.py`/`test_replay_signal_tools.py` if the SIGNAL class needs the tool listed.

**Interfaces:**
- `propose_decision(options: list[{key,label,detail?}], recommended_key: str, rationale: str)` SIGNAL tool, mounted with the other signals for engines with `durable_tool_surface`; handler (in-process, like `set_hold`): resolves the session's primary ref the way `set_hold` does, calls `decisions.propose(..., actor=f"session:{ctx.session_id}")`, records the audit, returns the proposal id and the instruction "end the turn with `mark_no_pr(\"awaiting-decision\", note=...)`". Blurb in `instruction.py` (`_BLURBS` must cover it — `test_session_tools_instruction.py` enforces). One eval case in `cases.jsonl`: a prompt with a genuine two-way choice → the session calls `propose_decision` then `mark_no_pr("awaiting-decision")`, never `hand_back`.
- `NO_PR_RESOLUTIONS = ("not-needed", "cancelled", "awaiting-decision")`; the tool schema enum and the Codex marker parser accept it (`resolution_from_text`).
- Runner no-PR branch: before the ladder logic, if the ended session's `pr_resolution == "awaiting-decision"` (read via the same session lookup the investigate settle uses — `git show hold-ladder-4b:server/automations/backlog_runner.py` shows `_backlog_investigation_state` if 4b has not merged; on master, read `Session.pr_resolution` through a small seam `session_resolution(session_id) -> str | None`), finalise `no_pr` with cause `decision_proposed`, release the row, emit `decision_proposed` event, and return — no `next_rung`, no `_give_up`, no bounce dial. The item is decision-held (HELD_KINDS) so the queue skips it as `held` until accept.
- Constitution check: `decision_proposed` is a CAUSE string on `DispatchOutcome.cause` (free text), not a new outcome; document it beside `DISPATCH_OUTCOMES`.

- [ ] Tests: tool handler writes the proposal + hold with actor `session:<id>` and refuses a second; instruction blurb present; `parse_mark_no_pr` accepts the new resolution and the Codex marker round-trips; loop test: session ended with `awaiting-decision` → outcome `no_pr`/`decision_proposed`, row released, `rec.escalations == []`, `rec.needs_humans == []`. Commit `feat(sessions): propose_decision tool; an awaiting-decision ending is neither a bounce nor a hand-off`.

---

### Task 4: The accepted decision reaches the next implement prompt

**Files:** Modify `server/automations/backlog_selection.py` (`Batch.decisions: dict[str, str] = field(default_factory=dict)`), `server/automations/backlog_grouping.py` (`plan_to_batch` passes through), `server/sessions/orchestrator.py` (`_backlog_spawn`: before building the prompt, `decisions = await run_sync(accepted_text_for_refs, batch.issues)` via a seam `_backlog_accepted_decisions(refs)`; set `batch = replace(batch, decisions=…)`), `server/automations/backlog_prompt.py` (`build_implement_prompt` renders a "## Decisions already taken" block listing each ref's text VERBATIM, only when non-empty); Tests: `test_backlog_prompt.py`, `test_automation_spawn_model.py` (or the spawn test file), `test_pm_orchestrator_seams.py`.

- [ ] Tests: prompt with one decision renders the block verbatim and the same prompt without decisions is byte-identical to today's; seam returns the newest accepted text per ref; spawn test with a stubbed seam sees the text in the composed prompt. Commit `feat(runner): the next implement run carries the accepted decision verbatim`.

---

### Task 5: The assistant tool and its policy rows

**Files:** Modify `server/assistant/tools.py` (`ToolSpec("resolve_decision", ToolClass.MUTATION, …)`, handler: validate `decision in ("accept","reject")`, `proposal_id` opaque, `chosen_key` required for accept → ONE `api.post(f"/api/pm/decisions/{pid}/{decision}", json=…)`), `server/assistant/policy.py` (allowlist rows for `GET /api/pm/decisions` and `POST /api/pm/decisions/[A-Za-z0-9-]+/(accept|reject)`; NOT the propose route — the assistant does not propose), `docs/superpowers/evals/assistant/cases.jsonl` (one case); Tests: `test_assistant_tools.py`, `test_assistant_conformance.py`, `test_assistant_policy.py` (a `False` row for `POST /api/pm/work-items/{id}/decisions` — the assistant token must be 403 there — and `True` rows for the two allowed).

- [ ] Commit `feat(assistant): resolve_decision`.

---

### Task 6: The inbox card, the dialog link, docs, gates

**Files:** `web/src/api/triage.ts` (`DecisionProposal` type; `fetchDecisions`), `web/src/api/hooks.ts` (`useDecisions` polling like `useTriage`, `useAcceptDecision`, `useRejectDecision` with `PM_MUTATION_META`, invalidating triage/decisions/pm views), `web/src/features/roadmap/inbox/` (`triageSelectors.pendingProposals` includes decisions via a discriminated `kind`; `InboxScopeBody` lists both, same ordering/keys; `DecisionCard.tsx` + `DecisionDetail.tsx` or a branch in `ProposalDetail.tsx`: radio group of options, recommendation preselected, rationale, `ConsequenceList` accept/reject, Accept (chosen key) / Reject), `web/src/shell/useNavBadges.ts` (badge counts both through the ONE selector), `web/src/features/roadmap/detail/IssueDispositionDialog.tsx` (decision hold → "Awaiting your decision" + a link to the inbox), `web/src/features/products/vocab.ts` (label), tests beside each. Docs: `docs/product/features/product-management-layer.md` (decision proposals section), `autopilot-backlog-runner.md` (awaiting-decision ending), `docs/product/features/session-tools.md` (`propose_decision`), `docs/product/features/ai-assistant.md` (`resolve_decision`), spec §14 (anything deferred).
- Gates: web full; server `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `python scripts/brand_ratchet.py`; the controller runs `server/tests/` detached.
- [ ] Commit `feat(roadmap): decision proposals in the inbox; docs`.

## Self-review

**Spec §6 coverage:** create endpoint + transaction (T1/T2); session tool → exactly the endpoint's logic (T3, in-process per today's pattern — ruling in the ledger); `mark_no_pr` "awaiting decision" + cause `decision_proposed` (T3); inbox list beside classification proposals, same ordering/keys, card with single-choice + recommendation + rationale + Accept/Reject (T6); Accept clears the hold, records the key, next prompt verbatim (T2/T4), comment on the issue (T2); Reject → owner-action hold by the person (T1/T2); explainer two-column (T2); assistant `resolve_decision` + deny-by-default allowlist (T5); notification via the existing poll/ws/badge channel (T6, no new kind). §7 dialog link (T6). §8 provenance rules (T1: only the decisions route sets/clears decision holds; T5: `False` row).

**Type consistency:** `decisions.propose/accept/reject/accepted_text_for_refs/pending_list` (T1) used in T2, T3, T4; `decision_guidance(policy=)` (T2) used by the list route; `Batch.decisions` (T4) read by `build_implement_prompt`; `NO_PR_RESOLUTIONS` (T3) read by the runner and the tool schema.

**Placeholders:** the web card's exact markup and the eval-case prompts are the implementers' with the content fixed; everything else names its values.
