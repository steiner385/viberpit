# Release Milestones Phase 2 (Manage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the live milestone ladder manageable. Owners and agents can tick manual conditions with evidence, record owner decisions and asks, edit a milestone's status line, and advance the current milestone, either when its gate is met or with a recorded owner override. Repo-computed numbers also start to measure, through `ci_metric`, which the product's CI publishes.

**Architecture:** One service module, `server/pm/milestone_actions.py`, holds every write rule: validation, the lock, the gate check, the override rule and the audit. Thin routes in `server/api/pm_milestones.py` call it, and Phase 3's MCP tools will reach it through those routes. Notes get their own table. Open asks feed the single needs-you count in `server/pm/tree.py`, the `roadmap_needs_you` list, and the web Needs-you list.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic · React + TS, React Query, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-05-release-milestones-design.md` (M4, M8–M11, §6, §7, §9, §10). The Phase 1 plan is `docs/superpowers/plans/2026-11-05-release-milestones.md`. Phase 1 has merged (#3316).

## Spec amendment (made in this PR)

**M9 "owner" is redefined.** The original definition, "the human logins in `trusted_authors` via `attention.owner_logins`", can't be checked. A caller is a tailnet identity (an email), while `trusted_authors` holds GitHub logins and user ids, and nothing maps one to the other. `owner_logins` is documented as display-only and "never a trust decision".

The new definition: an owner action needs `actor_class(identity) == "person"` (`server/pm/holds.py`) **and** a direct human request, meaning `request.state.via == "api"`. It is an allowlist, so it fails closed. The tailnet allowlist already limits who reaches the app at all.

The rule applies to:
- override advance;
- unticking or re-ticking a condition someone else ticked;
- resolving or dropping an ask someone else created.

Every other write needs only `actor_class == "person"`, which includes a session delegating on behalf of its human owner (`session_triage` sets `via="session"` with the person's identity).

## Global Constraints

- Milestone tables write **aware UTC** (`server.db.models._utcnow`) and normalize reads with `server.time_iso.aware_utc` (Phase 1 ruling; asyncpg would shift naive values by the host offset).
- New columns are nullable except PKs, FKs and the discriminator `kind`. Revision `0106_milestone_notes`, with `down_revision` set to the newest revision on `origin/master` when it is written (`0105_release_milestones` as of writing).
- Add the new model to `_REQUIRED_EXTRAS`, `_FK_LINKS` and `RECONCILE_EXEMPT` as those tests require, and run the Postgres suite.
- Every write is audited with `server/audit.py:record_audit(db, type="pm.milestone.<verb>", identity=…, detail=…, via=…)`, in the same transaction as the write.
- Stale or errored readings are `unknown`, never `met`. Advance without override requires `gate_met(read-time statuses)`.
- Do not change the definition from any write path. The YAML in the product repo is the only definition (M2).
- Product-agnostic code. US English copy. The server gate is all four steps. The web gate is typecheck, lint, the full test suite, build and budget. The bundle budget is at its ceiling, so new web code goes in the lazy Roadmap chunk (`web/src/api/milestones.ts` and `web/src/features/milestones/`).
- The cancel rule: use `cancel_and_reap`, and never swallow outer cancellation.

## Review Focus

1. **Two people advance at once.** The second gets a 409 `stale_current`, never a double advance. Pinned in Task 3. The lock is a no-op on SQLite, so the test drives `expected_current` staleness explicitly.
2. **Advancing the last milestone.** It becomes `done`, no milestone is current, and the view says the ladder is complete. Advancing again returns a 409 `ladder_complete`. Pinned in Tasks 3 and 5.
3. **A session tries to override** (`via="session"` with a person identity). It gets a 403 `override_requires_owner`. Pinned in Task 3.
4. **A malformed or stale `metrics.json`** (missing key, non-numeric value, expired artifact, no successful run). The condition reads `unknown` with a specific error, never a crash and never `met`. Pinned in Task 1.
5. **The current milestone's next rung was removed from the YAML** (`removed_at` set). Advance skips removed rungs. Pinned in Task 3.

---

## File Structure

| File | Responsibility |
|---|---|
| `server/github/actions_api.py` (modify) | `metrics_artifact(repo, run_id, *, run=None, run_bytes=None, sleep=…) -> dict \| None` |
| `server/pm/milestone_refresh.py` (modify) | Read `ci_metric` instead of the Phase 1 error |
| `server/automations/milestones_refresh.py` + `server/sessions/orchestrator.py` (modify) | Wire the artifact reader |
| `docs/product/features/release-milestones.md` | The capability doc, including the CI metrics contract (spec §6) |
| `server/db/models.py` (modify), `server/db/migrations/versions/0106_milestone_notes.py` | `MilestoneNote` |
| `server/pm/milestone_actions.py` | All write rules: `tick_condition`, `set_status_note`, `add_note`, `update_note`, `advance` |
| `server/pm/milestone_asks.py` | `open_milestone_asks(db, project_ids)`, the single source for needs-you |
| `server/api/pm_milestones.py` (modify) | Thin write routes |
| `server/pm/milestone_view.py` (modify) | Notes, `ladderComplete`, `asksOpen` in the payload |
| `server/pm/tree.py`, `server/api/pm_tree.py`, `server/pm/session_triage.py` (modify) | Asks counted in needs-you |
| `web/src/api/milestones.ts` (modify) | Write fetchers and mutation hooks |
| `web/src/api/roadmap.ts`, `web/src/features/roadmap/detail/needsYou.ts` (modify) | `counts.milestoneAsks` |
| `web/src/features/milestones/*` | Tick dialog, notes panels, status-line edit, advance dialog |

---

### Task 1: `ci_metric` from a CI artifact, plus the capability doc

**Files:**
- Modify: `server/github/actions_api.py`, `server/pm/milestone_refresh.py`, `server/automations/milestones_refresh.py`, `server/sessions/orchestrator.py`
- Create: `docs/product/features/release-milestones.md`
- Test: `server/tests/test_actions_api_metrics_artifact.py`, `server/tests/test_pm_milestone_refresh.py` (extend), `server/tests/test_pm_featuredoc_lint.py` (existing; it must still pass over the repo)

**Interfaces:**
- Produces:
  - `METRICS_ARTIFACT = "viberpit-metrics"` and `METRICS_FILE = "metrics.json"`, in `actions_api.py`.
  - `metrics_artifact(repo: str, run_id: int, *, run=None, run_bytes=None, sleep=time.sleep) -> dict | None`. It lists `repos/{repo}/actions/runs/{run_id}/artifacts`, picks the non-expired artifact named `METRICS_ARTIFACT`, downloads it via `_fetch_artifact_zip`, and returns the parsed `metrics.json`. It returns `None` when the artifact is absent or expired.
  - It raises `ValueError("metrics.json: <reason>")` on a bad zip or bad JSON, or when `version != 1` or `metrics` is not a dict.
  - `refresh_ladder(…, read_metrics: Callable[[str, int], dict | None] | None = None)`. `None` keeps the old behavior for existing callers and tests.
- `ci_metric` reading rules (spec §6):
  - Use the newest **completed `success`** run of `workflow` on the default branch: `list_workflow_runs(repo, workflow, branch=<default branch>, limit=10)`, filtered to `conclusion == "success"`, newest first. Probe up to 3 runs for the artifact.
  - The default branch is the measure's `branch` param if given, else the repo's default branch. Fetch it once per repo per refresh through the existing `actions_api.repo_ci_meta` (check that it returns the default branch; otherwise read it from `gh api repos/{repo}` once).
  - `measured_at` is the run's `updated_at`.
  - The value is `{"n": float(metrics[metric])}`.
  - The errors are:
    - "no successful run of <workflow>"
    - "no viberpit-metrics artifact in the last 3 successful runs"
    - "metric '<m>' missing"
    - "metric '<m>' is not a number"
    - "metrics.json: <reason>"
  - Read each distinct `(repo, workflow, branch)` once per refresh, even when several conditions use it.
  - `numbers` entries of kind `ci_metric` (no op) are `info`.
- Remove `CI_METRIC_PHASE1_ERROR` and every reference to it.
- `docs/product/features/release-milestones.md`:
  - Frontmatter: `slug: release-milestones`, `parent: product-management-layer`, `title: Release milestones`, `state: delivering`, and `links.prs` listing #3302, #3304 and #3316.
  - Body sections, modeled on `product-management-layer.md`: Problem, Target users, Desired outcome, Success metrics, Non-goals, Notes, Evidence.
  - Under Notes: the file schema (a short form of spec §5, linking the spec), the **CI metrics contract** (artifact name, `metrics.json` shape, freshness, error meanings), and a minimal example workflow:

```yaml
name: Milestone metrics
on: { push: { branches: [main] }, schedule: [{ cron: '0 6 * * *' }], workflow_dispatch: {} }
jobs:
  metrics:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          n=$(./scripts/count-something.sh)
          printf '{"version":1,"metrics":{"my_metric":%s},"sha":"%s","generated_at":"%s"}\n' \
            "$n" "$GITHUB_SHA" "$(date -u +%FT%TZ)" > metrics.json
      - uses: actions/upload-artifact@v4
        with: { name: viberpit-metrics, path: metrics.json, retention-days: 14 }
```

- [ ] **Step 1: Failing tests.**
  - In `test_actions_api_metrics_artifact.py`, using a fake `run`/`run_bytes` and an in-memory zip:
    - `test_returns_parsed_metrics`
    - `test_absent_artifact_returns_none`
    - `test_expired_artifact_returns_none`
    - `test_bad_json_raises_value_error`
    - `test_wrong_version_raises`
  - In `test_pm_milestone_refresh.py`:
    - `test_ci_metric_reads_newest_successful_run`: value 293, op eq 0 → `unmet`, `measured_at` = the run's `updated_at`.
    - `test_ci_metric_skips_runs_without_artifact`
    - `test_ci_metric_missing_key_is_unknown_with_error`
    - `test_ci_metric_non_numeric_is_unknown`
    - `test_ci_metric_no_successful_run_is_unknown`
    - `test_ci_metric_workflow_read_once_for_two_conditions`
    - `test_ci_metric_number_is_info`
- [ ] **Step 2:** Run the tests and confirm they FAIL.
- [ ] **Step 3:** Implement, and wire `read_metrics=lambda repo, run_id: metrics_artifact(repo, run_id)` in the orchestrator's refresh call. It is called via `asyncio.to_thread`.
- [ ] **Step 4:** Write the capability doc. Run `pytest server/tests/test_pm_featuredoc_lint.py server/tests/test_pm_featuredoc.py`, plus any test that lints repo docs, and confirm they pass.
- [ ] **Step 5:** Run the test files from Step 1 and confirm they PASS. **Commit:** `feat(pm): ci_metric reads the product's viberpit-metrics artifact`

### Task 2: `milestone_notes` table

**Files:**
- Modify: `server/db/models.py`, `server/tests/test_postgres_compat.py`, `server/tests/test_schema_reconcile_coverage.py`
- Create: `server/db/migrations/versions/0106_milestone_notes.py`
- Test: `server/tests/test_pm_milestone_models.py` (extend)

**Interfaces:**
- `MilestoneNote` / `milestone_notes`:
  - `id` (uuid PK)
  - `ladder_id` (FK `milestone_ladders.id`, NOT NULL, indexed)
  - `milestone_key` (nullable: ladder-wide)
  - `kind` (NOT NULL; `decision | ask`)
  - `ref` (a short handle such as `D4`), `text`, `url`
  - `status`: decisions use `locked | proposed | superseded`; asks use `open | done | dropped`
  - `created_by`, `created_at`, `updated_at`, `resolved_by`, `resolved_at`
  - Every DateTime column is `DateTime(timezone=True)`.
  - Index `(ladder_id, kind, status)`.
- Constants live in `server/pm/milestone_actions.py` (Task 3): `NOTE_KINDS`, `DECISION_STATUSES` and `ASK_STATUSES`.

- [ ] **Step 1: Failing test.** `test_milestone_note_roundtrip(isolated_db)`: insert a decision and an ask, read them back, and check every column, including the aware `created_at` round-trip.
- [ ] **Step 2:** Run it and confirm it FAILS. **Step 3:** Implement the model and the revision, following the 0105 style and chaining note. **Step 4:** Run the model test, `test_alembic_single_head.py`, `test_schema_reconcile_coverage.py`, and the Postgres suite on a scratch DB, and confirm they PASS. **Step 5: Commit:** `feat(db): milestone_notes (0106)`

### Task 3: Write rules service and routes

**Files:**
- Create: `server/pm/milestone_actions.py`
- Modify: `server/api/pm_milestones.py`
- Test: `server/tests/test_pm_milestone_actions.py`, `server/tests/test_api_pm_milestones.py` (extend)

**Interfaces:**
- `class MilestoneActionError(Exception)` carries `status: int`, `code: str` and `detail: dict`. Routes map it to `HTTPException(status, {"error": code, **detail})`.
- `Actor(identity: str, via: str)`, plus:
  - `is_person(actor) -> bool`, defined as `holds.actor_class(identity) == "person"`;
  - `is_owner(actor) -> bool`, defined as `is_person(actor) and actor.via == "api"` (spec amendment M9; an allowlist, so it fails closed).
- Every function takes an `AsyncSession` and an `Actor`, writes audit rows, and does **not** commit. The route commits.
  - `tick_condition(db, ladder, milestone_key, condition_key, *, actor, met: bool | None = None, value: float | None = None, evidence: str, url: str | None) -> MilestoneMeasurement`
    - The condition must exist in the ladder definition with `kind == "manual"`. Otherwise it returns 400 `not_manual` or 404 `unknown_condition`.
    - It needs exactly one of `met`/`value`; a `value` is allowed only for a `numbers` entry. Otherwise it returns 400 `bad_tick`.
    - `evidence` must be non-empty after trimming, or it returns 400 `evidence_required`.
    - The actor must be a person, or it returns 403 `person_required`.
    - Changing a tick whose `manual_by` is another identity requires `is_owner`, or it returns 403 `owner_required`.
    - It upserts the measurement row:
      - scope = milestone key, or `numbers` for numbers;
      - `value = {"met": met}` or `{"n": value}`;
      - status from `evaluate`;
      - `measured_at = fetched_at = _utcnow()`;
      - `manual_by`, `manual_evidence` and `manual_url` set from the request.
    - Audit `pm.milestone.tick`.
  - `set_status_note(db, ladder, milestone_key, *, actor, text: str | None) -> Milestone`
    - It requires a person. Empty text clears the note. The maximum is 200 characters, or it returns 400 `too_long`.
    - Audit `pm.milestone.status_note`.
  - `add_note(db, ladder, *, actor, kind, text, ref=None, url=None, milestone_key=None, status=None) -> MilestoneNote`
    - It requires a person.
    - Validation:
      - `kind` must be in `NOTE_KINDS`;
      - `text` must be 1–500 characters;
      - `milestone_key`, if given, must exist and not be removed;
      - the default `status` is `locked` for a decision and `open` for an ask.
    - Audit `pm.milestone.note_add`.
  - `update_note(db, ladder, note_id, *, actor, status=None, text=None) -> MilestoneNote`
    - The status must be valid for the note's kind.
    - Moving an ask to `done`/`dropped` sets `resolved_by` and `resolved_at`; reopening it clears them.
    - Resolving or editing another identity's note requires `is_owner`.
    - Audit `pm.milestone.note_update`.
  - `advance(db, ladder, *, actor, expected_current: str | None, override_reason: str | None, now) -> AdvanceResult(from_key: str | None, to_key: str | None, override: bool)`
    - It locks the ladder's milestone rows with `select(Milestone).where(ladder_id=…).with_for_update()`.
    - It returns 409 `stale_current {current}` when the current key differs from `expected_current`.
    - Without a current milestone, advance means **start**: the first non-removed milestone by ordinal becomes current. If any milestone is `done`, it returns 409 `ladder_complete`.
    - With a current milestone, it computes the read-time statuses of the current gate (the same function the view uses; extract it from `milestone_view.py` as `read_time_gate(ladder, milestone, measurements, now) -> list[dict]` and reuse it in both places).
      - **If `gate_met`:** proceed. `override_reason` is ignored and must be absent; otherwise it returns 400 `override_not_needed`.
      - **Otherwise:** `override_reason` is required (it returns 409 `gate_unmet {unmet: [...]}` without one), it must be at least 10 characters after trimming (400 `reason_too_short`), and the actor must satisfy `is_owner` (403 `override_requires_owner`).
      - Set current → `done`, `exited_at = now`. With an override, set `exit_override = {by, reason, at, unmet: [{key, status, value}]}`.
      - The next non-removed milestone by ordinal becomes current, with `entered_at = now`. If none remains, no milestone is current (the ladder is complete).
    - It requires a person in every case (403 `person_required`).
    - Audit `pm.milestone.advance` with `{from, to, override, unmet}`.
- Routes, all `Depends(current_identity)` plus `request: Request` for `via` (`getattr(request.state, "via", None) or "api"`). Each runs one transaction and commits, returning 200 with the updated **full view payload** (the same body as GET), so the client replaces its cache:
  - `PUT /api/pm/projects/{slug}/milestones/{key}/conditions/{ckey}` with body `{met?: bool, value?: number, evidence: str, url?: str}`. The `numbers` panel uses `key="numbers"`.
  - `PATCH /api/pm/projects/{slug}/milestones/{key}` with body `{statusNote: str | null}`.
  - `POST /api/pm/projects/{slug}/milestones/notes` with body `{kind, text, ref?, url?, milestoneKey?, status?}`.
  - `PATCH /api/pm/projects/{slug}/milestones/notes/{note_id}` with body `{status?, text?}`.
  - `POST /api/pm/projects/{slug}/milestones/advance` with body `{expectedCurrent: str | null, overrideReason?: str}`.
  - Route paths must not collide: `notes` and `advance` are reserved milestone keys. Add both to the parser's reserved keys (`server/pm/milestone_def.py`) alongside `numbers`, with a parser test. Declare the static `notes`/`advance` routes before `/{key}` in the router.

- [ ] **Step 1: Failing service tests** (`isolated_db`, a ladder seeded through the Phase 1 sync seam from the fixture):
  - `test_tick_manual_condition_sets_met_with_evidence`
  - `test_tick_rejects_computed_condition`
  - `test_tick_requires_evidence`
  - `test_tick_numbers_value`
  - `test_retick_of_anothers_tick_needs_owner`
  - `test_session_actor_cannot_retick_anothers`
  - `test_status_note_set_and_clear`
  - `test_add_decision_defaults_locked`
  - `test_add_ask_defaults_open`
  - `test_resolve_ask_sets_resolved_fields`
  - `test_reopen_clears_resolved`
  - `test_resolve_anothers_ask_needs_owner`
  - `test_advance_when_gate_met`
  - `test_advance_unmet_without_reason_409_lists_unmet`
  - `test_advance_override_by_owner_records_snapshot`
  - `test_override_from_session_403`
  - `test_override_from_assistant_403`
  - `test_override_reason_too_short`
  - `test_override_not_needed_when_met`
  - `test_stale_expected_current_409`
  - `test_advance_skips_removed_next`
  - `test_advance_last_completes_ladder`
  - `test_advance_after_complete_409`
  - `test_start_when_none_current`
  - `test_unknown_condition_counts_as_unmet`
  - `test_every_write_audited`
- [ ] **Step 2: Failing route tests** (TestClient, as in Phase 1's API tests):
  - one happy path per route, returning the full view;
  - the error-mapping shape `{"error": code, …}`;
  - `via` taken from `request.state` (simulate `session` via a dependency override or the session_triage `_request` helper);
  - the `notes`/`advance` reserved-key parser test.
- [ ] **Step 3: Implement.** Extract `read_time_gate` from `milestone_view.py` without changing the GET output, so the existing Phase 1 view tests stay green.
- [ ] **Step 4:** Run the test files, `pytest server/tests -k milestone -n 4`, ruff and mypy, and confirm they PASS. **Step 5: Commit:** `feat(pm): milestone writes — ticks, notes, status line, advance with owner override`

### Task 4: Notes in the view, and asks in needs-you

**Files:**
- Create: `server/pm/milestone_asks.py`
- Modify: `server/pm/milestone_view.py`, `server/api/pm_milestones.py` (GET loads notes), `server/pm/tree.py`, `server/api/pm_tree.py`, `server/pm/session_triage.py`
- Test: `server/tests/test_pm_milestone_asks.py`, extend `test_api_pm_milestones.py`, the tree tests (grep `needs_you_total` in server/tests), the session_triage tests, and the hygiene sampler test (`product_attention_counts`)

**Interfaces:**
- `async open_milestone_asks(db, project_ids: Collection[str] | None = None) -> dict[str, list[MilestoneNote]]` returns open asks keyed by project id, oldest first. `None` means all projects. It is the only query for "open asks" (constitution VI).
- View payload additions:
  - `notes: {decisions: Note[], asks: Note[]}`, ladder-wide, each with `milestoneKey`. Decisions are ordered locked, then proposed, then superseded; asks are ordered open first, then by `created_at`.
  - `Note = {id, kind, ref, text, url, status, milestoneKey, createdBy, createdAt, resolvedBy, resolvedAt}`.
  - `ladder.ladderComplete: bool`: no current milestone and at least one done.
  - `ladder.asksOpen: int`.
  - `gateSummary` unchanged.
- Needs-you:
  - `tree.build_product_tree` sets `counts["milestoneAsks"] = n` from a new `ProductIn` field.
  - `needs_you_total` adds `counts.get("milestoneAsks", 0)`.
  - The product tier becomes needs-you when `milestoneAsks > 0`, consistent with how `awaitingTriage` is treated. Follow that code path.
  - `pm_tree.product_nodes` fills it with **one** grouped query.
  - `session_triage._needs_you` adds one row per open ask, with kind `milestone_ask` and `{product, text, milestoneKey, url, id}`, ordered after decisions in the `order` dict.

- [ ] **Step 1: Failing tests:**
  - `test_open_asks_grouped_by_project_oldest_first`
  - `test_done_and_dropped_asks_excluded`
  - `test_view_includes_notes_sorted`
  - `test_ladder_complete_flag`
  - `test_needs_you_total_counts_open_asks`
  - `test_product_attention_counts_include_asks` (the hygiene sampler stays equal to the badge)
  - `test_roadmap_needs_you_lists_milestone_asks`
- [ ] **Step 2–4:** Run them and confirm they FAIL, implement, then run them plus `pytest server/tests -k "milestone or pm_tree or tree or session_triage or hygiene" -n 4` and confirm they PASS. **Step 5: Commit:** `feat(pm): milestone asks count as needs-you; notes in the milestones view`

### Task 5: Web actions and panels

**Files:**
- Modify: `web/src/api/milestones.ts`, `web/src/api/roadmap.ts` (`ProductNode.counts.milestoneAsks?: number`), `web/src/features/roadmap/detail/needsYou.ts` (+ test), `web/src/features/milestones/MilestonesPage.tsx`, `MilestoneCard.tsx`, `ConditionRow.tsx`, `NumbersPanel.tsx`
- Create: `web/src/features/milestones/TickDialog.tsx`, `NotesPanel.tsx` (Needs you + Decisions), `AdvanceDialog.tsx`, `StatusNoteEditor.tsx`, each with a `.test.tsx`
- Modify: `web/e2e/milestones.spec.ts`

**Interfaces:**
- **Fetchers and mutation hooks** in `milestones.ts`:
  - `useTickCondition(slug)`, `useSetStatusNote(slug)`, `useAddNote(slug)`, `useUpdateNote(slug)`, `useAdvanceMilestone(slug)`.
  - On success each sets the query data to the returned view (`queryClient.setQueryData(milestonesKey(slug), view)`).
  - Errors surface the server's `{error}` code through a single copy map, `milestoneErrorCopy(code) -> string`, in `milestones.ts`. Codes: `stale_current`, `gate_unmet`, `override_requires_owner`, `reason_too_short`, `evidence_required`, `owner_required`, `person_required`, `ladder_complete`, `not_manual`, `bad_tick`, `too_long`. Anything else gets a generic fallback.
- **Needs-you:**
  - `needsYouCount` adds `counts.milestoneAsks ?? 0` per product, unscoped like `pseudo`.
  - The Needs-you list gets a row "N milestone asks" linking to the milestones page.
  - The Python and TS counts must stay identical; add a parity test case mirroring `test_needs_you_total_counts_open_asks`.
- **UI** (spec §9 actions):
  - **ConditionRow.** A manual condition gets a "Tick" / "Update" button that opens `TickDialog`. The dialog has Met / Not met (or a number field for a numbers entry), required evidence and an optional link. The row shows "by <who> · <evidence>".
  - **MilestoneCard.**
    - The current card gets "Advance" when `gateSummary.ready`, and "Advance with override…" otherwise.
    - `AdvanceDialog` lists the unmet and unknown conditions and needs a reason of at least 10 characters for an override.
    - On a 403 `override_requires_owner` it shows "Only the owner can override from the ViberPit app".
    - On a 409 `stale_current` it refetches and shows "Someone else advanced this ladder".
    - "Start" appears when no milestone is current and none is done.
    - A "Ladder complete" banner appears when `ladderComplete`.
  - **StatusNoteEditor.** Inline edit of the status line (≤200 characters).
  - **NotesPanel.**
    - A **Needs you** panel (open asks, current milestone first) with Done and Drop buttons.
    - A **Decisions** panel (locked, then proposed; superseded collapsed).
    - Each panel has an "Add" form with kind, ref, text, an optional link and an optional milestone.
    - The side column order is Needs you, Numbers, Decisions (spec §9).
- **Accessibility:** every dialog uses the existing dialog primitive (grep `web/src/ui/` for Dialog) with focus trap and labelled fields. Buttons have accessible names.

- [ ] **Step 1: Failing tests:**
  - one per component behavior above;
  - `needsYou.test.ts` counts asks;
  - `milestoneErrorCopy` covers every code;
  - the e2e spec covers ticking a manual condition and an override dialog round trip, with mocked PUT and POST responses.
- [ ] **Step 2–4:** Run them and confirm they FAIL, implement, then run them and confirm they PASS.
- [ ] **Step 5:** Run the full web gate. Grep `web/e2e` for changed strings and run `npx playwright test e2e/milestones.spec.ts e2e/roadmap.spec.ts`.
- [ ] **Step 6:** Visual check: screenshot the page with a payload that has notes and an override, at 1440 and 390 px. Check that there is no overflow and that dialogs fit at 390 px.
- [ ] **Step 7: Commit:** `feat(web): manage milestones — ticks, notes, status line, advance and override`
