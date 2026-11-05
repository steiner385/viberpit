# Release Milestones Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a product define gate-driven release milestones in a repo YAML file. ViberPit measures each gate condition live and shows the ladder on its own product page; later phases add writes, MCP tools and a one-page PDF export.

**Architecture:**
- **Sync:** pm-sync reads `docs/product/milestones.yaml` off the product's default branch, through the existing single-file git-plumbing seam. A pure planner diffs it against the DB rows, and a seam applies the plan.
- **Refresh:** a refresh automation batches one GraphQL query per ladder (sub-issues, search counts, ref states) and one REST run list per workflow. It writes timestamped measurement rows.
- **API:** an `/api/pm/projects/{slug}/milestones` route builds the view model. Freshness is computed when the model is read.
- **Web:** a React page at `/roadmap/:productSlug/milestones` renders the view model.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (SQLite + Postgres), Alembic, PyYAML, pydantic · React + Vite + TS, React Query, Tailwind, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-05-release-milestones-design.md` (approved 2026-11-05; amended in this PR, see "Spec amendments").

## Phasing

| Phase | PR | Content | Detail in this plan |
|---|---|---|---|
| 1 | vertical slice | Parser, models, sync, refresh for `sub_issues`/`issue_count`/`refs_closed`/`workflow_streak`/`manual`/`ladder_leaves`, GET + refresh API, read-only page, KinDash YAML in KinDash | **Full tasks (1–8)** |
| 2 | writes | `ci_metric` + contract doc; `milestone_notes` table; manual tick, notes, status line, advance with override API; UI actions; `open_milestone_asks` into needs-you (M11) | Task outline (9) |
| 3 | agents | `roadmap_milestones`, `roadmap_milestone_note`, `roadmap_milestone_tick`, `roadmap_milestone_advance` + evals; assistant read tool | Task outline (10) |
| 4 | export | Print stylesheet + one-page Playwright test | Task outline (11) |
| 5 | KinDash cut-over | Metrics workflow, seed notes, retire `roadmap.html` pipeline (KinDash PRs) | Task outline (12) |

Phases 2–5 each get their own detailed plan, written when the phase starts against the code that has merged by then. The outlines here fix their scope and interfaces.

## Spec amendments (made in this PR)

1. **§9 "Where":** the view is its **own route**, `/roadmap/:productSlug/milestones`, not an 8th tab on the product page. The product page already has 7 tabs, one past the 6-tab cap (`docs/PAGE-TABS.md` §4, with one recorded exception), and PAGE-TABS says a page past six "is two objects that want two routes". The product Overview gets one summary block, "Milestones: <current> · n of m gate conditions met", linking to the route. Products with no ladder show no block.
2. **§5 "Path":** v1 reads the fixed path `docs/product/milestones.yaml`. The `milestones_path` policy key is dropped (YAGNI; no product needs another path yet).
3. **§5 schema:** an optional top-level `start: <milestone key>` field. When a ladder row is **first created**, that milestone becomes `current` (`entered_at` = sync time). It is ignored afterward, because the current marker is DB state (M2). Without this field, Phase 1 could not show "you are here" for KinDash before Phase 2's advance exists.
4. **§7 leaves:** a leaf is *in flight* only when one of its `closedByPullRequestsReferences` PRs has state `OPEN`. GitHub returns merged closing PRs too (verified live against cairnea/KinDash#20191 on 2026-11-05).

## Global Constraints

- Product-agnostic code: no `KinDash`, `cairnea`, or KinDash label or workflow names outside `server/tests/fixtures/` and test files.
- Every new column is nullable except PKs, FKs and `key`. Use `server/db/clock.py:db_utc_now()`, never `func.now()`. All datetimes are naive UTC.
- One Alembic revision per phase that adds tables, with `down_revision` = the newest revision on `origin/master` at implementation time (it is `0102_output_styles` as of writing; re-chain if one lands first).
- New models get entries in `_REQUIRED_EXTRAS` (`server/tests/test_postgres_compat.py`) and, where needed, `RECONCILE_EXEMPT` (`server/tests/test_schema_reconcile_coverage.py`). Run the Postgres suite (`VIBERPIT_TEST_PG_URL`).
- Stale or errored measurements read as `unknown`, never `met` (spec §7 status rule).
- GitHub reads use GraphQL; REST only for workflow runs (no GraphQL path).
- Teardown uses `server/aio.py:cancel_and_reap()`. No `except (CancelledError, Exception)`.
- Server gate before every push: `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/` (`-n 4`, background, tee'd to the scratch dir, never two runs at once, `env -u` both `VIBERPIT_*` and `CCSWITCH_*` sessiond/daemon-role vars).
- Web gate: `npm run typecheck`, `npm run lint`, full `npm run test`, `npm run build`, `npm run budget`.
- US English in all copy. Never label a status "Done" on a session surface. Milestone states are `upcoming | current | done`, and that is fine here.
- Run `python scripts/brand_ratchet.py` before pushing; no `ccswitch` spellings in new code.

## Review Focus

1. **A malformed or half-edited YAML on the default branch.** Expected: the last good definition stays, a banner shows the error and commit, and no milestone rows are removed. Pinned in Task 3.
2. **A milestone renamed or reordered in YAML while it is current.** Expected: the current marker follows the key, the ordinal changes, and a removed key keeps its row with `removed_at`, plus the warning. Pinned in Task 3.
3. **GitHub fails mid-refresh** (rate limit, a 404 on one ref, the whole query erroring). Expected: affected rows get `error` set and read `unknown`; the other conditions keep their last values with true ages. Pinned in Task 5.
4. **The refresh loop has not run for a day** (daemon down). Expected: every GitHub-backed condition reads `unknown` because of `max_age`, not `met` with old data. Pinned in Task 4.
5. **An epic with more than 100 sub-issues, or a cross-repo sub-issue.** Expected: all leaves are counted (paginated) and the cross-repo leaf links to its own repo. Pinned in Task 5.

---

## File Structure (Phase 1)

| File | Responsibility |
|---|---|
| `server/pm/milestone_def.py` | Pure: YAML → `LadderDef` dataclasses; the closed measure-kind registry (fields, default `max_age`); `MilestoneDefError` |
| `server/pm/milestone_index.py` | Pure planner: `LadderDef` or error + existing rows → `LadderSyncPlan` |
| `server/pm/milestone_sync_seams.py` | DB seams: `load_ladder_rows(project_id)`, `apply_ladder_plan(project_id, plan)` |
| `server/pm/milestone_status.py` | Pure: evaluate a raw reading against a condition (`op`/`target`); read-time freshness → `met/unmet/unknown/info` |
| `server/pm/milestone_github.py` | Builds the per-ladder GraphQL document, parses the payload into readings; `{now-Nd}` macro; workflow-streak computation from a run list |
| `server/github/actions_api.py` (modify) | Add `list_workflow_runs(repo, workflow, *, branch, run=None, limit=30) -> list[dict]` |
| `server/pm/milestone_refresh.py` | `refresh_ladder(...)`: reads → measurement rows; one place for the rate limit |
| `server/automations/milestones_refresh.py` | `MilestonesRefreshLoop` + automation registration (10 min, primary-only) |
| `server/pm/milestone_view.py` | Pure-ish: DB rows → the view model dict served by the API |
| `server/api/pm_milestones.py` | `GET …/milestones`, `POST …/milestones/refresh` |
| `server/db/models.py` (modify) | `MilestoneLadder`, `Milestone`, `MilestoneMeasurement` |
| `server/db/migrations/versions/0103_release_milestones.py` | Revision |
| `server/automations/pm_sync.py` + `server/sessions/orchestrator.py` (modify) | New `_milestones_pass(view)` and its seams |
| `web/src/api/milestones.ts` | Types + `fetchMilestones`, `refreshMilestones` |
| `web/src/api/hooks.ts` (modify) | `QK.milestones(slug)`, `useMilestones`, `useRefreshMilestones` |
| `web/src/features/milestones/*` | `MilestonesPage`, `MilestoneCard`, `ConditionRow`, `LeavesBar`, `NumbersPanel`, `MilestonesSummaryBlock` |
| `server/tests/fixtures/milestones/ladder.yaml` | The KinDash ladder as a test fixture (the only KinDash-named data) |

---

### Task 1: Definition parser and measure-kind registry

**Files:**
- Create: `server/pm/milestone_def.py`, `server/tests/fixtures/milestones/ladder.yaml`
- Test: `server/tests/test_pm_milestone_def.py`

**Interfaces:**
- Produces:
  - `MEASURE_KINDS: dict[str, MeasureKindSpec]`, where `MeasureKindSpec(required: frozenset[str], optional: frozenset[str], default_max_age_s: int | None, gate_ok: bool)`. Kinds and default ages:
    - `sub_issues` 3600
    - `issue_count` 3600
    - `refs_closed` 3600
    - `workflow_streak` 129600
    - `ci_metric` 129600
    - `ladder_leaves` 3600 (`gate_ok=False`)
    - `manual` None
  - `@dataclass(frozen=True) Ref(repo: str, number: int)` with `.key -> "owner/name#N"`.
  - `Measure(kind: str, params: Mapping[str, Any], op: str | None, target: float | None, max_age_s: int | None)`.
  - `Condition(key, label, measure: Measure)`.
  - `Platform(name, off: bool, note: str | None)`.
  - `MilestoneDef(key, name, label, audience, platforms: tuple[Platform, ...], epics: tuple[Ref, ...], refs: tuple[Ref, ...], gate: tuple[Condition, ...])`.
  - `LadderDef(version: int, title: str, spec: str | None, start: str | None, milestones: tuple[MilestoneDef, ...], numbers: tuple[Condition, ...])`.
  - `parse_ladder(text: str, *, primary_repo: str) -> LadderDef`, which raises `MilestoneDefError(str)`.
  - `parse_ref(s: str, *, primary_repo: str) -> Ref` (accepts `#N` and `owner/name#N`).
  - `to_json(ladder: LadderDef) -> dict` and `from_json(d: dict) -> LadderDef` (round-trip; stored in `milestone_ladders.definition`).

- [ ] **Step 1: Write the fixture.** Translate spec §12 into `ladder.yaml`, with all four milestones:
  - keys `household`, `friendly`, `community`, `public`; labels `Phase 0`–`Phase 3`; `start: household`; `epics: ['#20191']` … `['#20194']`, with `primary_repo` = `cairnea/KinDash`.
  - **Household gate:**
    - rubric → `ci_metric`, workflow `milestone-metrics.yml`, metric `rubric_missing_live`, `op: eq`, `target: 0`;
    - lane fixes → `refs_closed` `['#20200','#20201','#20202','#20203','#20204','#20205','#20206','#20207','#20208']`;
    - Nightly → `workflow_streak`, workflow `nightly.yml`, branch `main`, target 3;
    - consent → `refs_closed` `['#20234','#19215','#19230']`.
  - **Friendly families gate:** setup `manual` · week-two `manual` · `p0` `issue_count` `'is:issue is:open label:"priority:P0-stability"'` eq 0 · `critical-7d` `issue_count` `'is:issue label:"type:bug" label:"priority:P1-security" created:>={now-7d}'` eq 0 · `cohort-bugs` `issue_count` `'is:issue is:open label:"cohort:phase-1"'` eq 0.
  - **Community cohort gate:** the four D7 metrics `manual` + `p0` as above.
  - **Public self-serve gate:** three `manual` conditions.
  - **Numbers:** `rubric-gap` (`ci_metric`, no op), `leaves` (`ladder_leaves`), `outside-families` (`manual`), `defects-by-ci` (`manual`).
  - Copy names, audiences and platform chips verbatim from the KinDash research (spec §12 table), including `{name: Kiosk, off: true, note: not yet}` on Friendly families and `{name: iOS, off: true, note: after launch, funded by revenue}` on Public.

- [ ] **Step 2: Write failing tests.**

```python
def test_parses_fixture_ladder():
    lad = parse_ladder(FIXTURE.read_text(), primary_repo="cairnea/KinDash")
    assert [m.key for m in lad.milestones] == ["household", "friendly", "community", "public"]
    assert lad.start == "household"
    hh = lad.milestones[0]
    assert hh.epics == (Ref("cairnea/KinDash", 20191),)
    assert [c.key for c in hh.gate] == ["rubric", "lane-fixes", "nightly", "consent"]
    nightly = hh.gate[2].measure
    assert (nightly.kind, nightly.target, nightly.op) == ("workflow_streak", 3, "ge")
    assert lad.milestones[1].platforms[-1] == Platform("Kiosk", True, "not yet")

def test_round_trips_through_json():
    lad = parse_ladder(FIXTURE.read_text(), primary_repo="cairnea/KinDash")
    assert from_json(to_json(lad)) == lad

@pytest.mark.parametrize("yaml_text, msg", [
    ("version: 2\nmilestones: []", "version"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A}, {key: a, name: B}]", "duplicate milestone key 'a'"),
    ("version: 1\ntitle: t\nmilestones: [{key: A!, name: A}]", "key"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: nope}}]}]", "unknown measure kind 'nope'"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, colour: red}]", "unknown key 'colour'"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: issue_count, query: q}}]}]", "needs op and target"),
    ("version: 1\ntitle: t\nnumbers: [{key: n, label: N, measure: {kind: ladder_leaves, op: eq, target: 1}}]\nmilestones: []", "ladder_leaves"),
    ("version: 1\ntitle: t\nstart: zzz\nmilestones: [{key: a, name: A}]", "start"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, epics: ['nope']}]", "ref"),
    (": : :", "YAML"),
])
def test_rejects(yaml_text, msg):
    with pytest.raises(MilestoneDefError, match=msg):
        parse_ladder(yaml_text, primary_repo="o/r")
```

- [ ] **Step 3: Run.** `pytest server/tests/test_pm_milestone_def.py -v` → FAIL (import error).
- [ ] **Step 4: Implement** `server/pm/milestone_def.py`.
  - Use `yaml.safe_load` and wrap its error as `MilestoneDefError("YAML: …")`.
  - Validation is strict: an unknown key at any level raises. Keys match `^[a-z0-9][a-z0-9-]*$`.
  - `workflow_streak` defaults `op` to `ge`; `sub_issues` and `refs_closed` default to "all closed" (`op`/`target` None means "all closed").
  - A gate condition of a `gate_ok` kind other than `manual`, `sub_issues` or `refs_closed` must have `op` and `target`.
  - `numbers` may omit `op`.
  - Gate condition keys are unique per milestone; number keys are unique.
- [ ] **Step 5: Run.** Same command → PASS. Then `ruff check server/pm/milestone_def.py && mypy server/pm/milestone_def.py --ignore-missing-imports`.
- [ ] **Step 6: Commit.** `feat(pm): milestone ladder definition parser`

### Task 2: Models and revision

**Files:**
- Modify: `server/db/models.py` (after `AttentionSample`)
- Create: `server/db/migrations/versions/0103_release_milestones.py`
- Modify: `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`), `server/tests/test_schema_reconcile_coverage.py` (`RECONCILE_EXEMPT`, only if needed)
- Test: `server/tests/test_pm_milestone_models.py`

**Interfaces:**
- Produces three tables (columns exactly as spec §7, minus `milestone_notes`, which is Phase 2):
  - `MilestoneLadder` / `milestone_ladders`: `id` uuid PK, `project_id` FK `projects.id` **unique**, `source_path`, `source_sha`, `synced_at`, `title`, `spec_path`, `definition` JSON, `parse_error`, `parse_error_sha`, `refresh_requested_at` (a watermark like `ProjectRepo.sync_requested_at`, see `server/pm/sync_request.py`).
  - `Milestone` / `milestones`: `id`, `ladder_id` FK, `key` NOT NULL, `ordinal` int, `name`, `label`, `audience`, `platforms` JSON, `epics` JSON, `refs` JSON, `gate` JSON, `state` String, `entered_at`, `exited_at`, `exit_override` JSON, `status_note`, `removed_at`. UniqueConstraint(`ladder_id`, `key`).
  - `MilestoneMeasurement` / `milestone_measurements`: `id`, `ladder_id` FK, `scope` NOT NULL (a milestone key, `numbers`, or `leaves:<milestone key>`), `key` NOT NULL, `status`, `value` JSON, `target` JSON, `measured_at`, `fetched_at`, `error`, `source_url`, `manual_by`, `manual_evidence`, `manual_url`. UniqueConstraint(`ladder_id`, `scope`, `key`).
- The leaf list for milestone `m` is stored as a measurement row with `scope="leaves:<m>"`, `key="leaves"`, and `value={"done": n, "total": n, "items": [{repo, number, title, url, state, prs: [{number, url}]}]}`. Spec §7 says leaves are cached as a measurement; this pins the shape.

- [ ] **Step 1: Failing test** `test_milestone_tables_roundtrip(isolated_db)`. Insert a project, a ladder, two milestones and one measurement. Assert the unique constraints raise `IntegrityError` on a duplicate `(ladder_id, key)` and a duplicate `(ladder_id, scope, key)`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the models, then the revision with `upgrade()` creating all three tables and `downgrade()` dropping them in reverse order. Put the chaining note in the docstring, as in `0102_output_styles.py`.
- [ ] **Step 4: Run** the model test, `pytest server/tests/test_alembic_single_head.py server/tests/test_schema_reconcile_coverage.py -v`, and the Postgres suite (`VIBERPIT_TEST_PG_URL=… pytest server/tests/test_postgres_compat.py`; see memory "Running the Postgres suite locally") → PASS.
- [ ] **Step 5: Commit.** `feat(db): release milestone tables (0103)`

### Task 3: Sync planner, seams and the pm-sync pass

**Files:**
- Create: `server/pm/milestone_index.py`, `server/pm/milestone_sync_seams.py`
- Modify: `server/automations/pm_sync.py` (ctor params + `_milestones_pass`, called right after `_governance_check(view)` at both call sites, ~:644 and ~:693), `server/sessions/orchestrator.py` (wire the seams next to `read_constitution=` at ~:1595; the read uses `_pm_sync_read_repo_file(repo, "docs/product/milestones.yaml")`)
- Test: `server/tests/test_pm_milestone_index.py`, `server/tests/test_pm_sync_milestones.py`

**Interfaces:**
- Consumes: `parse_ladder`, `to_json`, `LadderDef` (Task 1); the models (Task 2).
- Produces:
  - `MILESTONES_PATH = "docs/product/milestones.yaml"` in `milestone_def.py`.
  - `@dataclass(frozen=True) MilestoneRowView(key: str, state: str | None, removed: bool)`.
  - `LadderRowsView(exists: bool, milestones: tuple[MilestoneRowView, ...])`.
  - `@dataclass(frozen=True) LadderSyncPlan(create_ladder: bool, ladder_fields: dict, parse_error: str | None, upserts: tuple[dict, ...], remove_keys: tuple[str, ...], revive_keys: tuple[str, ...], set_current: str | None, delete_ladder: bool)`.
  - `plan_ladder_sync(rows: LadderRowsView, *, text: str | None, sha: str, primary_repo: str, now: datetime) -> LadderSyncPlan`. `text is None` means the file is absent.
  - `load_ladder_rows(project_id) -> LadderRowsView` and `apply_ladder_plan(project_id, plan) -> None` in the seams module.
- Rules:
  - **File absent and no ladder:** no-op.
  - **File absent and a ladder exists:** `delete_ladder=False`. Set `parse_error="definition file removed"` and keep everything; deleting history needs an explicit owner action, which is a non-goal for v1.
  - **Parse error:** set `parse_error` and `parse_error_sha` only; no milestone changes.
  - **Good parse:**
    - clear `parse_error`;
    - upsert every milestone by key (ordinal = file index; definition fields replaced; `state`, `entered_at`, `exited_at`, `exit_override` and `status_note` never touched by sync);
    - set `removed_at` on keys missing from the file and clear it on keys that came back;
    - new milestones get `state="upcoming"`;
    - if `create_ladder` and `start`, set `set_current=start`.

- [ ] **Step 1: Failing planner tests** (pure, no DB):
  - `test_first_sync_creates_ladder_and_sets_start_current`
  - `test_parse_error_keeps_last_good` (asserts `upserts == ()` and `parse_error` contains the message)
  - `test_removed_key_marks_removed_and_current_survives` (current `friendly` removed → in `remove_keys`, `set_current is None`)
  - `test_reorder_changes_ordinal_only`
  - `test_start_ignored_after_creation`
  - `test_file_removed_sets_error_keeps_rows`
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `plan_ladder_sync`. **Step 4: Run** → PASS.
- [ ] **Step 5: Failing pass test** `test_pm_sync_milestones.py`, modeled on the existing constitution-pass tests (grep `read_constitution` in `server/tests/`).
  - Use a fake `read_milestones` returning the fixture text and `load`/`apply` seams backed by `isolated_db`.
  - Assert that after one tick there are 4 `milestones` rows with `household` current.
  - Assert that a raising `read_milestones` sets `_observation_degraded` and leaves rows untouched.
  - Assert that a non-primary repo view is skipped.
- [ ] **Step 6: Implement** the `_milestones_pass(view)` best-effort pass.
  - It mirrors `_governance_check`: same failure posture, using `log_gh_failure`.
  - The seams default to `None`, so existing pm-sync tests are unaffected.
  - The pass needs the head SHA for `source_sha`. Extend the read seam to return `(text, sha)` via a new orchestrator method `_pm_sync_read_milestones(repo) -> tuple[str | None, str]` that resolves the ref once with `git rev-parse` and then `git show <sha>:<path>`. Do not change `_pm_sync_read_repo_file`'s contract.
- [ ] **Step 7: Run** both test files plus `pytest server/tests -k pm_sync -n 4` → PASS. **Step 8: Commit.** `feat(pm): sync milestone ladders from the product repo`

### Task 4: Condition evaluation and read-time freshness

**Files:**
- Create: `server/pm/milestone_status.py`
- Test: `server/tests/test_pm_milestone_status.py`

**Interfaces:**
- Consumes: `Measure`, `MEASURE_KINDS` (Task 1).
- Produces:
  - `Reading(value: dict | None, measured_at: datetime | None, error: str | None, source_url: str | None)`.
  - `evaluate(measure: Measure, reading: Reading) -> str` returns the stored status (`met | unmet | unknown | info`):
    - an error or a None value gives `unknown`;
    - no op on a numbers entry gives `info`;
    - `{done,total}` with no op is met iff `done == total and total > 0`;
    - otherwise apply `op` to `value["n"]` (or to `total-done` open count for `sub_issues` with op).
  - `status_at(stored: str, measured_at: datetime | None, max_age_s: int | None, now: datetime) -> str` returns `unknown` when the measurement is stale, otherwise the stored status.
  - `gate_met(statuses: Iterable[str]) -> bool` is all `met` and non-empty.

- [ ] **Step 1: Failing tests:**
  - `test_eq_zero_met`
  - `test_eq_zero_unmet_with_293`
  - `test_ge_streak`
  - `test_all_closed_requires_nonempty` (0/0 → unmet)
  - `test_error_is_unknown`
  - `test_stale_reads_unknown` (measured 37 h ago, `max_age` 36 h → unknown, even if stored `met`)
  - `test_manual_never_stale` (None `max_age`)
  - `test_gate_met_empty_is_false`
  - `test_gate_met_with_one_unknown_is_false`
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS. **Step 5: Commit.** `feat(pm): milestone condition evaluation`

### Task 5: GitHub readings (GraphQL batch + workflow runs)

**Files:**
- Create: `server/pm/milestone_github.py`
- Modify: `server/github/actions_api.py`
- Test: `server/tests/test_pm_milestone_github.py`, `server/tests/test_actions_api_workflow_runs.py`

**Interfaces:**
- Consumes: `LadderDef`, `Ref`, `Reading` (Tasks 1, 4).
- Produces:
  - `expand_query_macros(q: str, now: datetime) -> str`: replaces `{now-<N>d}` with an ISO date (`YYYY-MM-DD`, UTC).
  - `build_ladder_query(ladder: LadderDef, *, primary_repo: str, now: datetime, cursors: Mapping[str, str] | None = None) -> tuple[str, dict[str, Target]]`.
    - It returns a GraphQL document and the alias → target map.
    - Aliases are `s0..` (search `issueCount`, with `repo:<primary>` prepended unless the query names a `repo:`), `r0..` (`issueOrPullRequest(number:)` → `__typename`, `state`, `url`), and `e0..` (epic `subIssues(first:100, after:)` → `totalCount`, `pageInfo`, and nodes with `number title url state repository{nameWithOwner} closedByPullRequestsReferences(first:5, includeClosedPrs:false){nodes{number url state}}`).
    - Refs are grouped by repo under `repository(owner:,name:)` aliases.
    - `Target` is a small dataclass naming `(scope, key)` or `epic:<repo#n>`.
  - `parse_ladder_payload(payload: dict, targets: dict[str, Target], *, now: datetime) -> tuple[dict[tuple[str, str], Reading], dict[str, list[dict]], dict[str, str]]`. It returns readings by `(scope, key)`, the leaf items by epic ref key, and next-page cursors by epic alias.
    - A missing or null alias gives a `Reading(error="not found or no access")`.
    - A leaf's `prs` keeps only PRs with `state == "OPEN"` (spec amendment 4).
  - `streak_from_runs(runs: list[dict]) -> tuple[int, datetime | None, str | None]`. Runs are newest-first. Skip `conclusion in {"skipped", "cancelled", None}` and status not `completed`. Count leading `success`. Return `(count, measured_at = newest counted-or-breaking run's updated_at, url of newest run)`.
  - `actions_api.list_workflow_runs(repo: str, workflow: str, *, branch: str, run=None, limit: int = 30) -> list[dict]`. It calls `gh api repos/{repo}/actions/workflows/{workflow}/runs?branch=…&per_page=limit&exclude_pull_requests=true` and returns the dicts with `id, status, conclusion, event, updated_at, html_url`. It is synchronous, like its neighbors.

- [ ] **Step 1: Failing tests.**
  - **Recorded payload:** use a small recorded GraphQL payload modeled on the live response, as a fixture JSON in `server/tests/fixtures/milestones/graphql_payload.json`. Its shape must match the live probe of 2026-11-05: `subIssues.nodes[].closedByPullRequestsReferences.nodes[]` include MERGED PRs for closed issues.
  - `test_query_groups_refs_by_repo_and_expands_macros`: `{now-7d}` with now = 2026-11-05 → `created:>=2026-10-29`.
  - `test_parse_counts_and_states`
  - `test_leaf_in_flight_only_with_open_pr`
  - `test_missing_alias_is_error_reading`
  - `test_pagination_cursor_returned_when_has_next_page`
  - `test_cross_repo_leaf_keeps_its_repo`
  - `test_streak_ignores_skipped_and_cancelled`: [success, skipped, success, failure, success] → 2
  - `test_streak_zero_when_newest_failed`
  - `test_list_workflow_runs_builds_args` (fake `run`)
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS. **Step 5: Commit.** `feat(pm): batched GitHub readings for milestone gates`

### Task 6: Refresh service and loop

**Files:**
- Create: `server/pm/milestone_refresh.py`, `server/automations/milestones_refresh.py`
- Modify: `server/automations/registry.py` (register next to `NeedsYouHygiene`, with an `AutomationOversight` whose off/dry_run/on texts say: off = no refresh; dry_run = fetch and log, write nothing; on = write measurements), `server/sessions/orchestrator.py` (construct and start the loop the way `_needs_you_hygiene_loop`/`_task` are at ~:1936 and ~:3243; add the task attr to the teardown list at ~:1008)
- Test: `server/tests/test_pm_milestone_refresh.py`, `server/tests/test_milestones_refresh_loop.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4, 5. The GraphQL seam is the orchestrator's `_pm_gh_graphql(owner, query, partial_ok=True)`. With `partial_ok=True`, a bad ref errors only its alias, and that alias becomes an error reading rather than aborting the ladder (Review Focus 3).
- Produces:
  - `async refresh_ladder(project_id: str, *, graphql: Callable[[str, str], Awaitable[dict]], list_runs: Callable[..., list[dict]], now: datetime) -> RefreshResult(written: int, errors: int)`.
    - It loads the ladder definition (`from_json`), builds and runs the query (following epic cursors until done, at most 10 pages per epic), and calls `list_runs` once per distinct `(repo, workflow, branch)` via `asyncio.to_thread`.
    - It evaluates each condition and upserts `milestone_measurements` rows by `(ladder_id, scope, key)`, with `fetched_at=now`.
    - It writes `leaves:<m>` rows (union over the milestone's epics plus `refs`) and the `ladder_leaves` number.
    - `ci_metric` gets `Reading(error="ci_metric is measured from Phase 2")`.
    - `manual` rows are never written by refresh.
    - If the whole GraphQL call raises, every GitHub-backed row keeps its old value and `measured_at`, and gets `error` set. This is Review Focus 3, with true ages.
  - `REFRESH_MIN_INTERVAL_S = 60`; `async refresh_if_due(project_id, *, force: bool, …) -> RefreshResult | None` checks the newest `fetched_at` for the ladder.
  - `MilestonesRefreshLoop(ctx, read_config, list_ladder_projects, list_requested, refresh)` wakes every 15 s. It refreshes a ladder when its `refresh_requested_at` is newer than its newest `fetched_at`, or when its newest `fetched_at` is 600 s old. It runs on the primary only, and refreshes each ladder sequentially. One ladder failing logs a WARNING through `log_gh_failure` and continues.

- [ ] **Step 1: Failing service tests** (`isolated_db`, fake `graphql` returning the Task 5 fixture payload, fake `list_runs`):
  - `test_refresh_writes_conditions_and_leaves`: household `lane-fixes` reads 8/9 unmet; `leaves:household` total 18, 1 open; `nightly` from fake runs.
  - `test_graphql_failure_marks_errors_keeps_values`
  - `test_ci_metric_unknown_in_phase1`
  - `test_manual_rows_untouched`
  - `test_refresh_if_due_rate_limits`
  - `test_pagination_follows_cursor`
- [ ] **Step 2: Failing loop tests:** `test_loop_refreshes_each_ladder_and_survives_one_failure` and `test_loop_off_does_nothing` (follow `server/tests/test_needs_you_hygiene*.py` for context and seam fakes).
- [ ] **Step 3: Implement.** **Step 4: Run** both files plus `pytest server/tests/test_automation_registry*.py -v` → PASS. **Step 5: Commit.** `feat(pm): milestone measurement refresh loop`

### Task 7: View model and API

**Files:**
- Create: `server/pm/milestone_view.py`, `server/api/pm_milestones.py`
- Modify: `server/app.py` (`include_router` next to `pm_tree_router` at ~:1138)
- Test: `server/tests/test_api_pm_milestones.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces the `GET /api/pm/projects/{slug}/milestones` payload (camelCase, like the other `/api/pm` routes). It is 404 when the product doesn't exist, and `{"ladder": null}` when the product has no ladder.

```ts
{ ladder: { title, specPath, specUrl, sourcePath, sourceSha, syncedAt, parseError, parseErrorSha,
    currentKey: string | null, currentRemoved: boolean, updatedAt: string | null /* oldest fresh fetched_at */ },
  milestones: Array<{ key, label, name, audience, platforms: {name, off, note}[], state, enteredAt, exitedAt,
    statusNote, exitOverride, epics: {ref, url}[],
    gate: Array<{ key, label, kind, status /* read-time */, value, target, op, measuredAt, ageSeconds, error, sourceUrl, manualBy, manualEvidence }>,
    gateSummary: { met, total, unknown, ready: boolean },
    leaves: { done, inFlight, notStarted, total, measuredAt, items: Array<{ref, title, url, state: 'done'|'in_flight'|'not_started', prs: {number,url}[]}> } }>,
  numbers: Array<same shape as gate entries> }
```

- Builder: `build_milestones_view(ladder, milestones, measurements, *, now) -> dict`. It applies `status_at` with each condition's `max_age_s` (or the kind default), and drops `removed_at` milestones unless current.
- `POST …/milestones/refresh` calls `refresh_if_due(force=True)` through the orchestrator facade. It returns 202 `{"refreshed": bool, "retryAfter": s}`, and 429-style `{"refreshed": false}` within the rate window.
  - **Decision: a DB watermark, not an RPC.** The route stamps `milestone_ladders.refresh_requested_at = db_utc_now()`, like `server/pm/sync_request.py:request_project_sync`. The daemon's loop also wakes every 15 s, looks for ladders whose `refresh_requested_at` is newer than their newest `fetched_at`, and refreshes those. That avoids a new orchestrator facade method and an IPC RPC (the edge never runs the loop). The 60 s rate limit is enforced in the route against the newest `fetched_at`.
- Auth: `Depends(current_identity)`, as in `pm_tree.get_map`.

- [ ] **Step 1: Failing tests** (TestClient, `isolated_db`, rows seeded directly):
  - `test_get_returns_null_ladder_for_product_without_one`
  - `test_get_shapes_kindash_fixture`: current `household`; `gateSummary` `{met:1,total:4,unknown:1}` with seeded rows; leaves counts.
  - `test_stale_measurement_reported_unknown_with_age`
  - `test_parse_error_surfaces`
  - `test_refresh_sets_request_flag_and_rate_limits`
  - `test_404_unknown_product`
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS. **Step 5:** Run the **full server gate** (four steps; background, tee'd to the scratch dir). **Step 6: Commit.** `feat(api): GET/refresh product milestones`

### Task 8: Read-only Milestones page, Overview block, KinDash definition

**Files:**
- Create: `web/src/api/milestones.ts`, `web/src/features/milestones/MilestonesPage.tsx`, `MilestoneCard.tsx`, `ConditionRow.tsx`, `LeavesBar.tsx`, `NumbersPanel.tsx`, `MilestonesSummaryBlock.tsx` + a `.test.tsx` for each component
- Modify: `web/src/api/hooks.ts` (`QK.milestones = (slug) => ['pm','milestones',slug]`, `useMilestones(slug)` with 60 s poll, `useRefreshMilestones(slug)`), `web/src/features/roadmap/roadmapNavState.ts` (`MILESTONES_ROUTE = \`${ROADMAP_PATH}/:productSlug/milestones\``), `web/src/shell/routes.tsx` (lazy route), `web/src/features/roadmap/detail/ProductDetail.tsx` (Overview summary block; counts toward PAGE-TABS' at-most-three summary blocks, so check the existing count first, and replace rather than add if already at three; record the choice in the PR)
- KinDash: a PR to `cairnea/KinDash` adding `docs/product/milestones.yaml` (the fixture content), so the deployed ViberPit renders the live ladder

**Interfaces:**
- Consumes: the Task 7 payload. TS types mirror it exactly in `milestones.ts`.
- Layout per spec §9: header strip ("You are here: Phase 0 · Household gate", "1 of 4 gate conditions met · 1 unknown", "1 in flight", "Updated 4 min ago" + Refresh), the ladder (4 columns ≥1024 px, stack below), and a side panel with Numbers only in Phase 1. Needs-you and Decisions arrive in Phase 2.
- Status glyph + text, never color alone: `✔ Met`, `✖ Not met`, `? Unknown`, with the age ("3 h ago") and the error in a tooltip and an `aria-describedby`.
- Reuse existing primitives (`Banner`, `EmptyState`, chips, the progress-bar token set). Grep `web/src/ui/` before adding any component or token.

- [ ] **Step 1: Failing component tests:**
  - `ConditionRow` renders `? Unknown` with "37 h ago" for a stale row; `✔ Met` for met; the value "293 → 0" for a numeric condition with a target.
  - `LeavesBar` shows "17 of 18 done · 1 in flight".
  - `MilestoneCard` emphasizes current (`aria-current="step"`) and strikes through off platforms with their note.
  - `MilestonesPage` shows the parse-error banner, the "no ladder" `EmptyState` with the file path, and the header strip numbers.
  - `MilestonesSummaryBlock` is hidden when `ladder` is null.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Grep** `web/e2e` for any changed string (memory: e2e is not a required check). Add `web/e2e/milestones.spec.ts`: it mocks `GET …/milestones` with the fixture payload and asserts the page renders four cards and "You are here".
- [ ] **Step 6: Web gate:** `(cd web && npm run typecheck && npm run lint && npm run test && npm run build && npm run budget)`.
- [ ] **Step 7: Visual check.** Run the branch's Vite dev server against the live loopback API (memory: "Screenshot a branch against the live loopback API"). Screenshot `/roadmap/<kindash-slug>/milestones` at 1440 and 390 px, probe for horizontal overflow, and attach both to the PR via `share_artifact`.
- [ ] **Step 8: Commit, then open the KinDash PR** with `docs/product/milestones.yaml`. Use the KinDash repo's own PR rules (read its `.agents/instructions/core.md` first; never top-level `cd` into KinDash; use `git -C`).
- [ ] **Step 9: Phase 1 PR:** whole-branch review, then the PR with `Refs` the spec PR (#3302). Mark it ready and arm auto-merge.

---

## Phase outlines (each gets its own detailed plan)

### Task 9 (Phase 2): writes, `ci_metric`, needs-you
- **`ci_metric`:** `actions_api` lists the workflow's newest successful default-branch runs, finds the `viberpit-metrics` artifact, and parses `metrics.json` via `_fetch_artifact_zip`. `measured_at` = run `updated_at`. Errors as in spec §6. Contract doc: `docs/product/features/release-milestones.md`.
- **`milestone_notes` table:** revision 0104.
- **Routes (spec §10):** `PUT conditions/{ckey}`, `PATCH {key}`, `POST/PATCH notes`, `POST advance`.
  - `advance` has the `FOR UPDATE` lock and the `expected_current` 409.
  - The gate is checked with `gate_met` at read time.
  - An override requires `override_reason`. It is refused (403) unless the identity is a person in `attention.owner_logins` **and** `via ∉ {session, assistant}` (M9, M10).
  - Every route is audited as `pm.milestone.*`.
- **UI:** tick dialog (evidence required), note add/resolve, status line edit, Advance / Advance with override (the dialog lists unmet conditions), and the Needs-you and Decisions side panels.
- **`open_milestone_asks(db, project_ids) -> dict[str, list[MilestoneNote]]`:** consumed by `tree.needs_you_total`, `session_triage._needs_you` and `detail/needsYou.ts` (M11). Includes the hygiene sampler test.

### Task 10 (Phase 3): MCP tools
- **Specs:** three `ToolSpec`s in `server/sessions/tools/registry.py`, and handlers in `server/sessions/tools/roadmap.py` through `session_triage.invoke`.
- **Permissions:** `roadmap_milestone_tick` and `roadmap_milestone_advance` go in the human-in-the-loop list in `server/permission_rules/patterns.py`. `roadmap_milestone_note` is allow-listed (no card), and its owner check is skipped (the actor is the session).
- **Evals and docs:** an eval case per tool, and an update to `docs/product/features/session-tools.md`.
- **Assistant:** a read tool `get_milestones` in `server/assistant/tools.py` (one endpoint per handler), plus the parallel-assistant-tool counters (memory).

### Task 11 (Phase 4): export
- **Stylesheet:** `@media print` scoped to `MilestonesPage`, per spec §9 (caps, `@page letter landscape`, snapshot stamp, controls hidden), plus an Export PDF button calling `window.print()`.
- **One-page test:** a Playwright test with the mocked fixture payload, `page.emulateMedia({media:'print'})` + `page.pdf({format:'Letter', landscape:true})`, asserting the page count is 1. Count `/Type /Page\b` occurrences in the PDF bytes.

### Task 12 (Phase 5): KinDash cut-over (KinDash repo PRs)
- **Metrics workflow:** `.github/workflows/milestone-metrics.yml` runs `evidence-rubric-lint.sh --report .`, filters to `state: live` docs, counts `required missing`, and uploads `viberpit-metrics`.
- **Seed notes:** a session seeds D1–D15 and the three asks with `roadmap_milestone_note`.
- **Retire the HTML pipeline once at parity:** delete `roadmap.html`, `roadmap.pdf`, `render-roadmap.sh` and the roadmap README; rewrite the core.md rule; fix the stale spec path.
