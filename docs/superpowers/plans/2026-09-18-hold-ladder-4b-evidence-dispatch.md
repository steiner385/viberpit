# Hold Ladder — 4b: evidence dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An auto-filed error issue is born as an evidence hold, is dispatched by the Backlog Runner with an investigation prompt instead of the implement prompt, exits through one of four documented outcomes without ever counting as a bounce, and sibling reports of one error family are investigated together.

**Architecture:** No schema change. The mirror's upsert gives a NEW `auto-error` issue row an `evidence` hold (actor `automation:error-triage`); the triage-candidate predicate stops excluding evidence-held items. The queue read annotates candidates with their error family (derived from the linked `ErrorSignature`'s logger+event), and a deterministic pre-bucket turns evidence candidates into family groups before the LLM grouping sees the rest. `_backlog_spawn` picks the prompt and the `dispatch_kind` from the primary's `hold_kind`, records it on the runner row and the outcome row; the sweep settles an ended investigation from the issue's real state (GitHub) and the hold, never from PR state, and hands off through the give-up path when the investigation produced no exit. `hold_guidance` gains the evidence copy.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async · React+TS (one small dialog change) · pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` §5 (evidence dispatch), §3 (`dispatch_kind`), §8 (actor classes), §14 (follow-ups). Decision proposals (§6) are item 5.

## Global Constraints

- Constitution VI: ONE prompt module (`server/automations/backlog_prompt.py`) gains the investigate prompt beside the implement prompt; ONE hold writer (`server/pm/holds.py:set_hold` and the orchestrator's `_backlog_write_runner_hold`); ONE family derivation (`server/pm/error_family.py`, new, pure); ONE settle rule for investigations in the runner (`_settle_investigation`). No new tier ordering, no new hold kinds, no schema change (no migration).
- `dispatch_kind = "investigate"` rows never count as bounces or attempts (already enforced by `_implement_only()`); the sweep must never route an investigate dispatch through the ladder, the bounce dial, or the PR-Doctor branches.
- An evidence hold is queue-eligible (`HELD_KINDS` unchanged); an evidence item is never escalated onto the ladder (the `stronger_model` writer skips `evidence` holds); the give-up writer may replace an evidence hold with `owner_action` (that IS exit 4).
- Audit every hold write: `pm.hold.set {workItemId, kind: "evidence", reason, actorClass: "system"}` identity `automation:error-triage`, `via="pm_sync"`.
- Reads of GitHub go through the automation context's existing `gh` seams (best-effort; unknown → retry next tick, never a hand-off on unknown).
- No absolute date literals in tests; no new `CCSWITCH_` literals (brand ratchet ceiling is exact); commit trailer literally `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; messages via file + `git commit -F`; keep "deploy/release/rollout/promote" and "prod/production" apart in any command text.
- Test invocation: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …` (foreground, never the whole `server/tests/`).
- Branch `hold-ladder-4b`, stacked on `hold-ladder-4` (PR #1961) until it merges, then `git rebase --onto origin/master <4a tip> hold-ladder-4b`.

## File structure

| File | Responsibility |
|---|---|
| `server/pm/error_family.py` (new) | `family_key(logger, event) -> str | None`; `families_for_refs(db, refs) -> dict[ref, family]` (join `ErrorSignature.issue_number` to `<repo>#<n>` refs) |
| `server/sessions/orchestrator.py` | `_pm_sync_upsert_items`: evidence hold at birth; triage predicates accept non-HELD kinds; `_backlog_query_queue` annotates `family` + `hold_kind`; `_backlog_refresh_groups` pre-buckets; `_backlog_spawn` picks prompt + kind; `_backlog_investigation_state` seam; `_backlog_write_runner_hold` skips evidence for `stronger_model` |
| `server/automations/backlog_prompt.py` | `build_investigate_prompt(batch, *, engine)` |
| `server/automations/backlog_grouping.py` | `family_groups(candidates, *, max_k)` pure pre-bucket; `propose_groups` receives only non-evidence candidates |
| `server/automations/backlog_runner.py` | runner row `dispatch_kind`; `_settle_dispatch` passes it to `record_outcome`; `_sweep_one` routes investigate dispatches to `_settle_investigation`; `investigation_state` seam |
| `server/automations/context.py` | `issue_state(ref) -> "open" | "closed" | None` best-effort via `gh issue view` |
| `server/pm/explain.py` | `hold_guidance` evidence copy |
| `web/src/features/roadmap/detail/IssueDispositionDialog.tsx` | render guidance copy for an evidence hold (no buttons) |
| docs | runner doc, PM-layer doc, spec §14 |

---

### Task 1: Evidence hold at birth, and the classify predicate accepts it

**Files:**
- Modify: `server/sessions/orchestrator.py:~7571-7602` (`_pm_sync_upsert_items`), `~7921-7925` (`_pm_triage_list_pending`), `~8143-8148` (`_pm_triage_count_state`)
- Modify: `server/pm/holds.py` (a named actor constant `ERROR_TRIAGE_ACTOR = "automation:error-triage"` and `set_evidence_hold(wi, *, reason, actor, now)`)
- Test: `server/tests/test_pm_sync_upsert_holds.py` (new), `server/tests/test_pm_triage_candidates_holds.py` (new or extend the existing triage-list test file — read `server/tests/test_pm_api.py` / grep `_pm_triage_list_pending`)

**Interfaces:**
- `holds.ERROR_TRIAGE_ACTOR = "automation:error-triage"` (system class via `SYSTEM_PREFIXES`); `holds.EVIDENCE_BIRTH_REASON = "auto-filed error report: investigate before implementing"`.
- `holds.set_evidence_hold(wi, *, reason, actor, now)` = `set_hold(wi, kind="evidence", reason=reason, actor=actor, now=now)`.
- In `_pm_sync_upsert_items`, ONLY on the new-row branch (`existing is None` → `WorkItem(...)` created): if `item.kind == "issue"` and `"auto-error" in (item.labels or [])` (the label constant lives in `server/automations/error_triage.py:LABEL` — import it, do not re-spell) and the issue is open → `set_evidence_hold(...)` with `actor=ERROR_TRIAGE_ACTOR`, `reason=EVIDENCE_BIRTH_REASON`, then the audit row (`pm.hold.set`, identity `ERROR_TRIAGE_ACTOR`, `via="pm_sync"`, detail `{workItemId, kind, reason, actorClass: "system"}` — copy the shape from `_backlog_write_runner_hold`). Never on the existing-row branch: an item whose investigation cleared the hold must not be re-held by the next sync tick.
- Triage predicates: replace `WorkItem.hold_kind.is_(None)` with `or_(WorkItem.hold_kind.is_(None), WorkItem.hold_kind.not_in(HELD_KINDS))` in BOTH `_pm_triage_list_pending` and `_pm_triage_count_state` (import `HELD_KINDS` from `server/pm/desired_state.py`). Factor the clause into one small helper `_classifiable_hold_clause()` next to them so the two cannot drift.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_sync_upsert_holds.py` — build the orchestrator the way `test_pm_sync_projects.py` builds it for `_pm_sync_upsert_items` (read that file for the fixture: `init_db(db_url=...)`, `SessionOrchestrator.__new__`, the item shape the upsert takes — a `SimpleNamespace`/dataclass with `repo, number, kind, state, title, body, labels`). Cases:

```python
async def test_new_auto_error_issue_is_born_as_an_evidence_hold(tmp_path):
    # upsert one NEW open issue with labels ["auto-error", "source:server"]
    # → WorkItem.hold_kind == "evidence", hold_by == "automation:error-triage",
    #   hold_reason == EVIDENCE_BIRTH_REASON, hold_set_at set;
    #   exactly one AuditEvent type "pm.hold.set" with detail.kind == "evidence",
    #   detail.actorClass == "system", identity == "automation:error-triage"

async def test_existing_auto_error_issue_is_never_re_held(tmp_path):
    # upsert the same issue twice; between the ticks clear the hold (clear_hold(wi, actor=...))
    # → after the second upsert hold_kind is still None, and still one audit row

async def test_non_auto_error_issue_and_closed_issue_get_no_hold(tmp_path):
    # labels ["bug"] → no hold; labels ["auto-error"] but state "closed" → no hold
```

Triage predicate test — find the existing test that seeds `WorkItem` rows and calls `_pm_triage_list_pending` / `_pm_triage_count_state` (grep the names in `server/tests/`); add one case seeding three unclassified open issues: no hold, `evidence` hold, `owner_action` hold → the list returns the first two and the count is 2.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_sync_upsert_holds.py <triage test file> -q`
Expected: FAIL (`ImportError: set_evidence_hold` / list returns 1).

- [ ] **Step 3: Implement** per the Interfaces block.

- [ ] **Step 4: Run** the two files plus `server/tests/test_pm_sync_projects.py server/tests/test_pm_holds.py server/tests/test_pm_api.py -q`; then `ruff check server/`, `mypy server/sessions/orchestrator.py server/pm/holds.py --ignore-missing-imports`, `python scripts/brand_ratchet.py`.
Expected: PASS / clean.

- [ ] **Step 5: Commit** `feat(pm): an auto-filed error issue is born as an evidence hold; evidence items still get classified` (trailer).

---

### Task 2: The investigation prompt

**Files:**
- Modify: `server/automations/backlog_prompt.py` (beside `build_implement_prompt`)
- Test: `server/tests/test_backlog_prompt.py`

**Interfaces:**
- `build_investigate_prompt(batch, *, engine) -> str` — pure. Same header/ref block conventions as `build_implement_prompt` (read it; reuse its private helpers for the ref list and repo line — do not duplicate the formatting). Body (exact wording is the implementer's, the CONTENT is fixed):
  1. Frame: "These are auto-filed error reports. Investigate; do not implement. Read-only on code: open no PR, push nothing."
  2. Exactly one of four exits, each naming the concrete tool/command:
     - **Close as noise**: comment the reason on the issue (`gh issue comment`), close it (`gh issue close`), then `mark_no_pr("not-needed", note="closed as noise: <why>")`.
     - **Consolidate**: pick one primary; for each sibling comment "duplicate of #<primary>" and close it; leave the primary open; then `mark_no_pr("not-needed", note="consolidated into #<primary>")`.
     - **Convert to a fix**: edit the primary's body (`gh issue edit --body-file`) to add a "## Findings" section (root cause, reproduction, the code path), clear the hold with the `set_hold` tool (`kind` null), then `mark_no_pr("not-needed", note="converted to a fix")`. The item re-enters as an implement candidate.
     - **Escalate**: when only the owner can supply something (access, a decision, a reproduction only they have): write what is needed into the body's "## Findings" section, then `mark_no_pr("cancelled", note="needs owner: <the one specific thing>")`. The runner hands the issue to the owner with that note as the reason.
  3. "If you end without one of these exits, the runner hands the issue to the owner." (This is the no-exit rule in Task 4.)
  4. Engine gate: for Codex (no `mark_no_pr` tool) the same four exits use the marker form the engine supports — read how `build_implement_prompt` varies by engine and mirror it; if the implement prompt has no engine variance for completion signals, neither does this one.

- [ ] **Step 1: Write the failing tests** in `test_backlog_prompt.py` (copy the shape of the implement-prompt tests): the prompt names all four exits, mentions `mark_no_pr` and `set_hold`, contains the primary ref and every sibling ref, says "no PR"/read-only, and does NOT contain the implement prompt's PR instructions (assert a phrase the implement prompt has, e.g. its "open a pull request" wording, is absent).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `server/tests/test_backlog_prompt.py -q`, ruff, mypy on the module.
- [ ] **Step 5: Commit** `feat(runner): the investigation prompt` (trailer).

---

### Task 3: The spawn chooses the prompt and records the dispatch kind; the escalate writer skips evidence

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_backlog_spawn` ~6700-6840: the existing `hold_kind` read for `batch.primary` at ~6710; the `build_implement_prompt` call at ~6830; `_backlog_write_runner_hold` guard ~6209)
- Modify: `server/automations/backlog_runner.py` (the claim-row write ~1226-1240 gains `"dispatch_kind"`; `_settle_dispatch` ~1340-1360 passes `dispatch_kind=state.get("dispatch_kind") or "implement"` to `record_outcome`; the spawn seam's return/`intent` must carry the kind back to the loop — read how `model` travels from `_backlog_spawn` to the runner row and carry `dispatch_kind` the same way)
- Test: `server/tests/test_automation_spawn_model.py` (spawn tests), `server/tests/test_backlog_runner_loop.py`, `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:**
- In `_backlog_spawn`: `primary_hold_kind` (already read) → `dispatch_kind = "investigate" if primary_hold_kind == "evidence" else "implement"`; `prompt = build_investigate_prompt(batch, engine=…) if investigate else build_implement_prompt(…)`; the ladder rung step is SKIPPED for an investigate dispatch (an evidence item never climbs); the spawned-event detail gains `"dispatch_kind"`; the value returned to the loop carries `dispatch_kind` so the runner row stores it.
- `record_outcome(..., dispatch_kind=<row value>)` from `_settle_dispatch`; `_outcome_fallback` (the §13 fallback row) passes the same.
- `_backlog_write_runner_hold`: for `kind == "stronger_model"`, also skip when `wi.hold_kind == "evidence"` (log `hold_kept`); for `kind == "owner_action"` an evidence hold IS replaced (exit 4 / no-exit hand-off).

- [ ] **Step 1: Write the failing tests**
  - spawn test: primary with `hold_kind="evidence"` → the composed prompt is the investigate prompt (assert a phrase unique to it), the event detail has `dispatch_kind == "investigate"`, no `ladder_rung`, and the returned intent carries `dispatch_kind`; primary without a hold → `"implement"`.
  - loop test: after a spawn whose intent carries `dispatch_kind="investigate"`, the runner row has `dispatch_kind == "investigate"` and the `DispatchOutcome` row written by `_settle_dispatch` has `dispatch_kind == "investigate"`.
  - seam test: `_backlog_escalate` over an issue holding `evidence` leaves it `evidence` (no audit); `_backlog_needs_human` over the same issue replaces it with `owner_action` (one audit).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the three files + `server/tests/test_backlog_runner_e2e.py server/tests/test_automation_context_outcomes.py -q`; ruff; mypy on the two modules.
- [ ] **Step 5: Commit** `feat(runner): an evidence-held primary is dispatched as an investigation` (trailer).

---

### Task 4: The sweep settles an investigation from the issue's state, not the PR's

**Files:**
- Modify: `server/automations/backlog_runner.py` (`_sweep_one`: a new early branch before the PR-Doctor and no-PR branches; `_settle_investigation`; seam `investigation_state`)
- Modify: `server/automations/context.py` (`issue_state(ref) -> str | None` best-effort via the existing `gh` runner used by the PR-status helper — read `pr_status` and mirror it: `gh issue view <n> --repo <repo> --json state`)
- Modify: `server/sessions/orchestrator.py` (seam `_backlog_investigation_state(target) -> dict`: `{"issue_state": ctx.issue_state(primary) , "hold_kind": wi.hold_kind, "hold_by": wi.hold_by, "no_pr_note": <the session's recorded no-PR note or None>}` — find where `mark_no_pr(resolution, note)` persists its note (`server/sessions/completion.py`; the session record's completion fields) and read it by session id)
- Test: `server/tests/test_backlog_runner_loop.py`, `server/tests/test_pm_orchestrator_seams.py`, `server/tests/test_automation_context_outcomes.py` (or the context's gh-seam test file)

**Interfaces:**
- `_sweep_one`, after the ownership/legacy guards and the `_session_ever_ran` check, and BEFORE any PR-Doctor / no-PR / abandoned logic: `if st.get("dispatch_kind") == "investigate" and session ended: return await self._settle_investigation(target, st)`. The ladder, the bounce dial, PR-Doctor `gave_up`, and `abandoned` never see an investigate row.
- `_settle_investigation(target, st)`:
  1. `info = await self._investigation_state(target)` (seam; default stub returns `{}`).
  2. `issue_state is None` (unknown) → log `backlog_runner.investigation_state_unknown` and RETURN without touching the row (next tick retries).
  3. `issue_state == "closed"` (exits 1/2) → finalise the outcome row as `no_pr` with cause `"investigation: issue closed"` and release the row (`_write_state(target, st, None, outcome=…)`), emit an AutomationEvent `kind="investigated"` detail `{exit: "closed", note}`.
  4. `issue_state == "open"` and `hold_kind != "evidence"` (exit 3: the session cleared the hold; or a person moved it) → finalise `no_pr` cause `"investigation: converted to a fix"`, release the row, emit `investigated {exit: "converted"}`. The item re-enters as an implement candidate on the next refill.
  5. `issue_state == "open"` and `hold_kind == "evidence"` (exit 4 or no exit) → `reason = f"investigation: {no_pr_note}" if no_pr_note else "investigation ended without an exit"`; finalise `no_pr` with that cause; then `await self._give_up(target, st, reason)` (owner-action hold + comment + `gave_up` row + retry — the existing path; it REPLACES the evidence hold, which is the intended exit 4). Emit `investigated {exit: "escalated"|"no_exit"}`.
  The outcome row's `dispatch_kind` stays `investigate` (set at spawn), so none of these count as bounces.
- `_session_ever_ran` false → the existing never-ran release (no penalty), unchanged.

- [ ] **Step 1: Write the failing tests** (loop harness: a runner row with `dispatch_kind: "investigate"`, `rec.investigation_state` seam returning each of the four `info` shapes; assert the row/outcome/`rec.needs_humans`/events per case; plus "unknown leaves the row untouched" and "the ladder seam is never called for an investigate row"). Context test for `issue_state` with a stubbed `gh` runner returning `{"state":"CLOSED"}` → `"closed"`, a failure → `None`. Seam test for `_backlog_investigation_state` reading the hold and the no-PR note from a seeded session.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the files + `server/tests/test_backlog_runner_e2e.py server/tests/test_backlog_runner_automation.py -q`; ruff; mypy on the three modules.
- [ ] **Step 5: Commit** `feat(runner): an ended investigation settles from the issue and the hold, never as a bounce` (trailer).

---

### Task 5: Family grouping — sibling reports become one investigation

**Files:**
- Create: `server/pm/error_family.py`
- Modify: `server/automations/backlog_grouping.py` (`family_groups`), `server/sessions/orchestrator.py` (`_backlog_query_queue` ~6950 candidate dict gains `hold_kind` and `family`; `_backlog_refresh_groups` ~6068-6098 pre-buckets)
- Test: `server/tests/test_error_family.py` (new), `server/tests/test_backlog_grouping.py`, `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:**
- `error_family.family_key(logger: str | None, event: str | None) -> str | None` — `f"{logger}:{event}"` when both are present, else `None` (no family: the item groups alone).
- `error_family.families_for_refs(db, refs: Sequence[str]) -> dict[str, str]` — sync helper (called under `run_sync` like the other pm helpers): select `ErrorSignature.issue_number, logger, event` where `issue_number` is in the numbers parsed from `refs` whose repo is the error-triage target repo (read how error_triage picks its filing repo: config/settings; the plain `<owner/name>#<n>` refs are compared on number AND repo); returns ref → family for those with a key.
- `_backlog_query_queue`: each candidate dict gains `"hold_kind": row.hold_kind` and, for evidence-held candidates only, `"family": families.get(ref)`.
- `backlog_grouping.family_groups(candidates, *, max_k) -> tuple[list[dict], list[dict]]` — pure: partitions candidates into evidence-held vs the rest; evidence candidates are bucketed by `family` (a missing family = its own bucket), each bucket becomes one group `{primary: <first by queue order>, refs: [...]}` capped at `max_k` (overflow forms further groups of the same family), and returns `(evidence_groups, remaining_candidates)`. Groups use the SAME plan shape `store_plan` persists (read `parse_group_plan`/`store_plan` for the dict shape — do not invent a second).
- `_backlog_refresh_groups`: `evidence_groups, rest = family_groups(candidates, max_k=…)`; `propose_groups` runs over `rest` only (an evidence item never shares a batch with an implement item); the stored plan = evidence groups + proposed groups.

- [ ] **Step 1: Write the failing tests**: pure `family_key`; `families_for_refs` over seeded `ErrorSignature` rows (two sharing logger+event, one different, one unfiled); `family_groups` with five evidence candidates in two families + two plain candidates and `max_k=2` → three evidence groups (2, 1 overflow… write the exact expected list) and the two plain candidates returned as `rest`; `_backlog_refresh_groups` seam test: the stored plan contains the family group first and `propose_groups` was called with only the non-evidence candidates (monkeypatch it).
- [ ] **Step 2: Run to verify they fail.** **Step 3: Implement.** **Step 4: Run** the files + `server/tests/test_backlog_grouping.py -q`; ruff; mypy. **Step 5: Commit** `feat(runner): sibling error reports are investigated together (family grouping)` (trailer).

---

### Task 6: Consequence copy, dialog line, docs, gates

**Files:**
- Modify: `server/pm/explain.py` (`hold_guidance`: evidence branch), `server/tests/test_pm_explain.py`
- Modify: `web/src/features/roadmap/detail/IssueDispositionDialog.tsx` (+ its test): when the hold is `evidence` and `guidance` carries copy, render it as a plain consequence line inside the hold panel (no buttons) using the existing `ConsequenceList` renderer — read how the owner_action guidance is rendered and reuse the component; never a second renderer.
- Modify: `docs/product/features/autopilot-backlog-runner.md` (an "Evidence dispatch" section: birth, prompt, the four exits, the no-exit hand-off, family grouping, not-a-bounce), `docs/product/features/product-management-layer.md` (holds paragraph: evidence = born at filing, queue-eligible, investigated), `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` §14 (one bullet: the issue-state read is a GitHub call per ended investigation; the family key is logger+event, not a stored column).

**Interfaces:**
- `hold_guidance(*, hold_kind="evidence", policy)` returns the same shape as the owner_action branch (read it) with machine copy: "The next run investigates this report instead of implementing it: it closes noise, consolidates duplicates, converts it into a fix, or hands it to you with what is needed. No pull request." — one entry, no buttons.

- [ ] **Step 1: Failing tests**: `test_pm_explain.py` evidence case; dialog test renders the copy for an evidence hold and no Retry/Implement buttons.
- [ ] **Step 2–3: Implement.**
- [ ] **Step 4: Gates**: server `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ -q`, `python scripts/brand_ratchet.py`; web `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` (full test run). The controller runs `server/tests/` detached.
- [ ] **Step 5: Commit** `feat(roadmap): evidence-hold consequence copy; docs for evidence dispatch` (trailer). The controller rebases onto master once #1961 lands, re-runs the gates, opens the PR (`Refs #1540`), arms auto-merge.

## Self-review

**Spec §5 coverage:** written at filing (born in the sync upsert of the auto-filed issue — T1; the filing automation itself never sees the WorkItem row, and the sync tick that mirrors the new issue is the first moment the row exists, so "at filing time" is satisfied within one sync interval); by sessions/people (already: `set_hold` tool + overlay); dispatched as an ordinary candidate with the investigation prompt (T2, T3); four exits with existing tools (T2 prompt, T4 settle); grouping by family with the per-dispatch cap (T5, `max_issues_per_pr`); not a bounce (`dispatch_kind` T3 + the sweep bypass T4); read-only, no PR, default tier (T2 wording, T3 skips the rung); consequence copy from explain.py (T6). Spec §14 promised the investigation tier question stays open — the prompt runs at the product default.

**Type consistency:** `build_investigate_prompt(batch, *, engine)` (T2) called in T3; `dispatch_kind` string travels spawn → intent → runner row → `record_outcome` (T3) → read by `_sweep_one` (T4); `investigation_state` seam returns `{issue_state, hold_kind, hold_by, no_pr_note}` (T4); `family_groups(candidates, *, max_k) -> (groups, rest)` (T5) consumed in `_backlog_refresh_groups`; `families_for_refs(db, refs) -> dict` (T5).

**Placeholders:** T2's exact prompt wording and T5's exact expected group list are left to the implementer with the content fixed; everything else names its values.
