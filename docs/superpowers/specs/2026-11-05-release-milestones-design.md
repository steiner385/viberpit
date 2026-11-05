# Release milestones: design

**Status:** proposed, awaiting owner review · **Date:** 2026-11-05 · **Reference fixture:** the KinDash
launch ladder (cairnea/KinDash epic #20190, phases #20191–#20194)

## 1. Problem

A product's significant release milestones (a launch ladder, a run of releases, a set of gates) live
outside ViberPit today, hand-kept per product. KinDash is the working example: four gate-driven phases,
each a GitHub epic with an exit gate, pictured in a hand-edited `roadmap.html` that a script renders to
PNG and PDF. Every session that changes launch state has to re-derive the numbers (sub-issue counts, the
evidence-rubric gap count, the Nightly green streak, which PRs are in flight), edit the HTML and
re-render. The picture goes stale between sessions, and agents spend real effort recomputing it.

ViberPit should own this. A product defines its milestones once, and the status stays live with no hand
edits.

## 2. Decisions

| # | Decision | Who decided |
|---|---|---|
| M1 | A milestone is a **new entity**, not a specialization of an initiative, stage or capability (§4). | Builder call, justified below |
| M2 | The **definition** (milestones, audience, platforms, gate conditions, how each is measured) lives in a **YAML file in the product repo**, synced by pm-sync like capability docs. **Live state** (current marker, manual ticks, notes, overrides, cached measurements) lives in the ViberPit DB. | Autopilot picked the recommended option; the **owner has not confirmed it** |
| M3 | ViberPit **never runs product-repo commands**. A number that needs repo code (e.g. the rubric gap count) is computed by the **product's own CI**, which publishes it as a workflow artifact. ViberPit only reads GitHub. | Autopilot picked this, **against the builder's recommendation** (run on the ViberPit host); the **owner has not confirmed it** |
| M4 | **Any session may advance** a milestone when every gate condition is measured *met* and fresh, behind the normal permission card. Advancing past an unmet or unknown gate is an **owner override**: a human in the web UI only, with a written reason, audited and shown on the view. | Autopilot picked the recommended option; the **owner has not confirmed it** |
| M5 | Milestones are **gate-driven with no dates**. The only times shown are when a milestone was entered or exited, and how old each measurement is. | Autopilot picked the recommended option; the **owner has not confirmed it** |
| M6 | Export is a **print stylesheet** plus `window.print()` from the view, not a server-side render (§9). | Builder call (the owner left it open) |
| M7 | This **amends roadmap-drilldown D5** ("no dates, no milestones"). D5 rejected calendars and timeline views. A gate-driven milestone ladder keeps that intent (no dates, no Gantt) and lifts only the "no milestones" clause. `2026-09-08-finish-line-persona-modes-design.md:200` gets the same note. | Builder call; follows from the owner's request |
| M8 | **Session writes are tiered.** Notes, the status line and refresh work from any session, unattended included, with no permission card; they are audited with the session as actor. **Ticking a manual gate condition** needs a card, because it can unlock an advance. Advance needs a card and a human-owned session (M4). | Autopilot picked this (not the builder's recommendation, which let ticks through without a card too); the **owner has not confirmed it** |
| M9 | **"Owner"** means the human logins in the product's `trusted_authors`, through `attention.owner_logins`, the existing definition. Override, untick-by-someone-else and resolving another person's ask are owner-only. Any allowlisted person may do the rest in the UI. | Builder call |
| M10 | **An override needs a direct human request.** The caller must be a person *and* the request must not be delegated (`via` not in `{session, assistant}`; the assistant's `ccas1:`/`ccas2:` calls count as assistant). A delegated session call carries the person's identity, so a person check alone would let a session override. | Builder call |
| M11 | **Open asks count as needs-you everywhere** through one function, `open_milestone_asks(db, project_ids)`. It feeds `tree.needs_you_total` (map badge and hygiene sampler), the `roadmap_needs_you` tool's list, and the web Needs-you tab. | Builder call |
| M12 | The plan is written against M2–M5 and M8 as stated. The owner's review of this spec is the confirmation point, and a change there is re-planned before Phase 1 is built. | Builder call |

## 3. User stories

- **US-1 Define.** As an owner, I commit `docs/product/milestones.yaml` to my product repo listing ordered
  milestones, each with a name, an audience line, platforms, linked epics, and gate conditions. Within one
  pm-sync tick, ViberPit shows the ladder.
- **US-2 See where we are.** As a busy owner opening the product's Milestones tab, I see at a glance which
  milestone we are in, which of its gate conditions are met, unmet or unknown (with values and ages), what
  is in flight, and what needs me.
- **US-3 Live numbers.** Leaf counts, open-label counts, the workflow green streak and CI-published
  metrics refresh on their own. I never edit a number by hand. Stale or failed measurements show as
  *unknown*, never as *met*.
- **US-4 Manual conditions.** The owner or an agent ticks a manual condition with evidence (text plus an
  optional link), and can untick it.
- **US-5 Notes.** The owner or an agent records owner decisions (locked or proposed) and owner asks.
  Open asks show in "Needs you" on the milestone view and in the product's existing needs-you count.
- **US-6 Advance.** A session or the owner advances the current milestone when its gate is fully met. The
  owner can override an unmet gate with a reason, which stays visible on the exited milestone.
- **US-7 Export.** I click Export PDF and get one clean landscape page of the live view. Nothing is saved
  anywhere.
- **US-8 Agents.** A session reads and updates milestones through `viberpit` MCP tools, so "update the
  roadmap" is a tool call.
- **US-9 KinDash.** The KinDash ladder is fully represented, and `roadmap.html`, `render-roadmap.sh` and the
  core.md hand-update rule are retired.

## 4. Why a new entity (M1)

The product layer's spine is Product (`Project`) → Capability (`Feature`) ← Initiative → Issue
(`WorkItem`). Nothing between Product and Initiative is ordered, and none of the candidates fits:

- **Initiative (incl. a program with phase children).** `stage` is the fixed SDLC sequence
  (intake→…→done), not a milestone ordinal. A program is never advanced (`advance_loaded` returns 409
  `has_children`). Gates are a global transition registry (`server/sdlc/gates.py`), not per-instance
  conditions. There is no "current" marker and no override path. Forcing milestones into initiatives would
  change the meaning of `stage` and `has_children` for every existing initiative.
- **Capability-tree node (`Feature`).** It describes what the product *does* (state proposed→live), not a
  release *gate*. The 5S plan already says time-boxed programs are not capabilities.
- **Stage.** Stages are an enum shared by every initiative, not product-defined data.

A milestone is therefore its own entity. It sits on the product, sits above initiatives, and
cross-references GitHub epics. It **reuses** existing pieces and does not copy them:

- epic leaves are read live from GitHub's `subIssues` (§7). The WorkItem mirror can't be trusted for this:
  its relations pass asks only for OPEN issues, so a child that closed before the pass saw it never gets a
  parent, and `_epic_progress_batch` undercounts. That is a pre-existing bug, filed as #3301;
- open owner asks feed `server/pm/attention.py`, the single needs-you definition;
- the YAML is read from the clone by pm-sync, following the feature-doc pattern (`feature_index.py`
  planner + `pm_sync._feature_index` apply, keep-last-good on parse error);
- artifacts are downloaded with `server/github/actions_api.py` `_fetch_artifact_zip`. A new helper in the
  same module lists one workflow's runs on a branch, keeping `updated_at`; the existing
  `list_completed_runs` is repo-wide, unfiltered and drops that field;
- MCP tools follow the `server/pm/session_triage.py` adapter: strict pydantic input, owner check, then a
  call to the HTTP handler in-process;
- writes are audited through the existing audit log, and the permission card comes from
  `permission_rules/patterns.py`.

No parallel "needs you", epic-progress, GitHub client or sync loop gets written.

## 5. The definition file (M2)

**Path.** The default is `docs/product/milestones.yaml` on the default branch of the product's primary
repo. A product can point elsewhere with the `milestones_path` key in `automation_policy`. There is one
ladder per product; multiple concurrent ladders are a non-goal.

**Schema (v1).**

```yaml
version: 1
title: Launch readiness                 # view heading
spec: docs/sdlc/launch-readiness-ladder-household-to-public-self-serve/spec.md   # optional link
milestones:                             # order = ladder order
  - key: household                      # stable id, [a-z0-9-]+, unique in file
    name: Household gate
    label: Phase 0                      # optional short prefix
    audience: The owner's family, onboarded and in daily use
    platforms:
      - Web
      - {name: Kiosk, off: true, note: not yet}
    epics: [cairnea/KinDash#20191]      # "#N" means the primary repo; leaves = sub-issues
    refs: [cairnea/KinDash#20523]       # optional extra issues/PRs whose state is shown
    gate:
      - key: rubric
        label: Evidence rubric satisfied for every live capability
        measure: {kind: ci_metric, workflow: milestone-metrics.yml, metric: rubric_missing_live, op: eq, target: 0}
      - key: nightly
        label: Nightly full suite green three runs running
        measure: {kind: workflow_streak, workflow: nightly.yml, branch: main, target: 3}
      - key: p0
        label: Zero open P0
        measure: {kind: issue_count, query: 'is:issue is:open label:"priority:P0-stability"', op: eq, target: 0}
      - key: consent
        label: Card-consent charge captured; processor DPAs on file
        measure: {kind: refs_closed, refs: ['#20234', '#19215', '#19230']}
      - key: setup
        label: Every family completes setup with no support call
        measure: {kind: manual}
numbers:                                # optional "numbers that matter" panel
  - key: rubric-gap
    label: evidence cells missing in live capabilities
    measure: {kind: ci_metric, workflow: milestone-metrics.yml, metric: rubric_missing_live}
  - key: leaves
    label: phase leaves closed
    measure: {kind: ladder_leaves}
  - key: outside-families
    label: outside families on production
    measure: {kind: manual}
```

**Measure kinds.** This is a closed, product-agnostic registry in one module. Each kind has a default
`max_age`, which a condition can override.

| kind | Reads | Value | Met when | Default `max_age` |
|---|---|---|---|---|
| `sub_issues` | GraphQL `subIssues` (all states, cross-repo) of `epic` (default: the milestone's first epic) | done / total | all closed, or `op`/`target` on open count | 1 h |
| `issue_count` | GitHub search `issueCount` for `query`, scoped to `repo` (default primary). `{now-7d}`-style relative date macros are expanded at query time | count | `op target` | 1 h |
| `refs_closed` | the state of each listed issue/PR (mirror first, else GraphQL) | closed / total | all closed or merged | 1 h |
| `workflow_streak` | completed runs of `workflow` on `branch`, newest first; events `schedule` and `workflow_dispatch` by default; `skipped` and `cancelled` are ignored | consecutive `success` count | `>= target` | 36 h |
| `ci_metric` | the newest successful default-branch run of `workflow` that has the artifact `viberpit-metrics`, file `metrics.json` (§6) | number | `op target` | 36 h |
| `ladder_leaves` | the sum of the milestones' leaves (numbers panel only) | done / total | n/a | 1 h |
| `manual` | the DB tick | met + evidence, or a number for the numbers panel | ticked met | none (no expiry in v1) |

`op` is one of `eq ne lt le gt ge`. A condition without `op`/`target` is display-only (it never blocks)
and is allowed only in `numbers`.

**Sync.** pm-sync reads the file at the default-branch head, as it does for feature docs. The parse is
strict: an unknown key or kind, a duplicate key, or a bad ref is a parse error. On error, ViberPit keeps
the last good definition, and the view shows a banner with the error and the commit. A milestone key
that disappears from the file keeps its DB row (`removed_at` set) so history and notes survive. If it was
current, the view warns that the current milestone is no longer defined.

**Ordering.** Ladder order is the file order. Reordering in the file reorders the view. The current
marker follows the key, not the position.

## 6. CI-published metrics (M3)

A product that wants a repo-computed number adds a workflow (any name; KinDash: `milestone-metrics.yml`)
that runs on push to the default branch and on a schedule, computes its numbers, and uploads one artifact:

- artifact name `viberpit-metrics`, containing `metrics.json`:
  `{"version": 1, "metrics": {"rubric_missing_live": 293}, "sha": "<commit>", "generated_at": "<ISO-8601>"}`

ViberPit picks the newest completed `success` run of that workflow on the default branch with that
artifact. The measurement time is the run's `updated_at` (its completion time), so the age reflects
when the number was computed. A missing artifact, an expired artifact, a missing key or a non-numeric
value is a measurement error, which shows as *unknown*. No product code runs on ViberPit hosts.

The contract is documented in `docs/product/features/release-milestones.md`, with a minimal example
workflow.

## 7. Data model (live state)

All new tables. Every column is nullable except PKs/FKs and the column that carries a key, so that
reconcile picks them up. There is one Alembic revision chained off the newest one at implementation
time. Adding models means updating `_REQUIRED_EXTRAS` / `RECONCILE_EXEMPT` and running the Postgres
suite (core.md).

**`milestone_ladders`**: one per product.
- `id`, `project_id` (FK, unique)
- `source_path`, `source_sha`, `synced_at`
- `title`, `spec_path`
- `definition` JSON: the last good parsed file, including `numbers`
- `parse_error`, `parse_error_sha`

**`milestones`**
- `id`, `ladder_id` (FK), `key` (unique per ladder)
- `ordinal`, `name`, `label`, `audience`
- `platforms` JSON, `epics` JSON, `refs` JSON, `gate` JSON (definition snapshot)
- `state` ∈ `upcoming | current | done`
- `entered_at`, `exited_at`
- `exit_override` JSON: `{by, reason, at, unmet: [{key, status, value}]}`, or null
- `status_note`: one free-text line, owner or agent editable ("the long phase by design")
- `removed_at`
- At most one `current` per ladder. A partial unique index enforces it where the dialect allows, and the
  advance path takes a row lock (`FOR UPDATE`) in any case.
- With no milestone current, the first milestone is shown as upcoming, and "advance" from none means
  "start the first".

**`milestone_measurements`**: the cache, one row per (ladder, scope, key).
- `scope` is a milestone key or `numbers`
- `status` ∈ `met | unmet | unknown | info`
- `value` JSON (`{n}` or `{done, total}`), `target` JSON
- `measured_at`: when the underlying data was produced (run time, search time); `fetched_at`
- `error`, `source_url` (deep link: search URL, run URL, epic URL)
- `manual_by`, `manual_evidence`, `manual_url` for manual rows

**Status rule.** The status is computed when a reader looks, not only when the row was stored:
`now - measured_at > max_age` or an `error` gives **unknown**. A gate is met only when **every** condition
is `met` and fresh.

**`milestone_notes`**: owner decisions and owner asks.
- `id`, `ladder_id`, `milestone_key` (nullable: ladder-wide)
- `kind` ∈ `decision | ask`
- `ref`: a short handle such as `D4`
- `text`, `url`
- `status`: decision ∈ `locked | proposed | superseded`; ask ∈ `open | done | dropped`
- `created_by`, `created_at`, `resolved_by`, `resolved_at`

Notes are not `DecisionProposal`s. A proposal is a pending multiple-choice question on a work item; these
are a log of decisions already taken, plus plain asks. When a choice is still open, the agent uses the
existing `propose_decision` on the relevant issue and records the outcome here as a `decision` note.

**Leaves and "in flight".** A milestone's leaves are its epics' sub-issues, fetched in the refresh's
batched GraphQL query (`subIssues(first:100)` with each child's state, title, url and its open
`closedByPullRequestsReferences`). The leaf list is cached as the `value` of a per-milestone `leaves`
measurement row. Each leaf is:
- **done**: closed;
- **in flight**: open, with an open PR whose closing references include it ("Fixes #N" and the like; a
  bare "Refs #N" mention does not count);
- **not started**: otherwise.

An epic with more than 100 sub-issues is paginated. If the epic can't be read (wrong ref, no access),
its leaves and any `sub_issues` condition show as *unknown*, with the GitHub error.

## 8. Refresh

- A **primary-only background loop** (same registration as the other pm loops) refreshes every ladder
  every 10 min. GitHub-backed kinds are batched per repo (one GraphQL query for all `issue_count` searches
  and ref states; one run list per workflow). It respects the shared `api_budget` limiter.
- A **refresh on demand** (`POST …/milestones/refresh`, a View button, an MCP tool) refreshes one ladder
  at once, rate-limited to one per minute per ladder.
- Every kind except `manual` reads GitHub only in the refresh; the view reads only the cache.

## 9. Web view and export

**Where.** A new **Milestones** tab on the product page (`ProductDetail` `TABS`, `?tab=milestones`).
It is hidden when the product has no ladder, and the Overview tile links to it. This follows the
reuse-the-chassis rule: no new top-level nav.

**Layout: at a glance first.**
1. **Header strip**: "You are here: Phase 0 · Household gate", "2 of 4 gate conditions met · 1 unknown",
   "1 in flight", "2 asks need you", plus "Updated 4 min ago · Refresh".
2. **Ladder**: one card per milestone, in order. That is four columns on desktop and a vertical stack on
   phone. The current card is emphasized, done cards are muted, and removed ones are hidden.
   - **Header**: label, name, audience line, platform chips (off chips struck through with their note),
     status ("Current · the long phase by design" / "Done · entered … exited …" / "Upcoming").
   - **Gate**: each condition has a ✔ met / ✖ unmet / ? unknown icon, the label, the value against its
     target ("293 → 0", "0 of 3 green"), its age, and a deep link. A manual condition shows who ticked it
     and the evidence. An override shows a banner on the exited card.
   - **Leaves bar**: done / in flight / not started, as segments and counts.
   - **Open leaves**: in-flight leaves first (with their PR), then not started. Done leaves collapse to a
     count. "+N more" links to the epic.
3. **Side panel**: **Needs you** (open asks, current milestone first), **Numbers**, and **Decisions**
   (locked, then proposed; superseded hidden).

**Actions (owner):** tick or untick a manual condition, add or resolve a note, edit the status line,
Advance, Advance with override (a reason is required; the dialog lists the unmet conditions), Refresh,
Export PDF.

**Export (M6).** "Export PDF" calls `window.print()`. A `@media print` stylesheet scoped to the
milestones view:
- hides the app shell;
- sets `@page { size: letter landscape; margin: 0.4in }`;
- lays the four cards in one row with the side panel below, and drops interactive controls;
- caps open-leaf lists at 4 per card ("+N more");
- stamps "Snapshot <local time> · <product>".

The user saves to PDF from the browser's print dialog. Nothing is written server-side.

*Why not a server-side render:* the edge has no headless Chromium, and adding one is a new
security-sensitive runtime dependency. A server render would show the same data the browser already
has. The print path also shows exactly what the owner sees.

*Risk:* browsers paginate differently. Mitigation: a Playwright e2e renders the view for the KinDash
fixture (Chromium `page.pdf({landscape: true, format: 'Letter'})`) and asserts one page. CSS keeps the
layout to one page with `break-inside: avoid` and the caps above. Unlimited content beyond the caps is
truncated, never paginated.

## 10. API

All under `/api/pm/projects/{slug}/milestones`, with the auth used by the existing `/api/pm` routes.

| Method / path | Purpose |
|---|---|
| `GET …` | The full view model: ladder meta, parse error, milestones with gate conditions (status computed at read time), leaves, notes, numbers, `current_key`, `needs_you_count` |
| `POST …/refresh` | Refresh measurements now (rate-limited) |
| `POST …/advance` | Body `{expected_current, override_reason?}`. Returns 409 `stale_current` if the current milestone changed, 409 `gate_unmet` with the unmet list if there is no override, and 403 if an override comes from a non-human or assistant identity |
| `PUT …/{key}/conditions/{ckey}` | Manual tick: `{met: bool, evidence, url?}`, or `{value: n}` for a manual number. Returns 400 for non-manual conditions |
| `PATCH …/{key}` | `{status_note}` |
| `POST …/notes` · `PATCH …/notes/{id}` | Create a note; change its status or text |

Every write is audited (`pm.milestone.*`).

## 11. MCP tools (session surface)

Added to `SESSION_TOOL_SPECS` beside the `roadmap_*` tools. Each one calls the handlers above
in-process through the `session_triage` adapter (constitution VI: one implementation).

| Tool | Class | Does |
|---|---|---|
| `roadmap_milestones(product)` | READ | The view model as compact text plus JSON: current milestone, each condition's status, value and age, open leaves, open asks, decisions |
| `roadmap_milestone_note(product, action, …)` | MUTATION, **no card** (M8): allow-listed in `permission_rules`, works for unattended sessions; actor = the session | `action` ∈ `note_add`, `note_set_status`, `status_note`, `refresh` |
| `roadmap_milestone_tick(product, milestone, condition, met \| value, evidence, url?)` | MUTATION, card | A manual condition only. It never changes the definition, which is the repo file; an agent edits the YAML by PR |
| `roadmap_milestone_advance(product, expected_current)` | MUTATION, card, human-in-the-loop (`patterns.py`) | Advances only if the gate is fully met. There is **no override parameter**: overrides are web-UI-only (M4) |

The card split is per tool name, so the permission rules stay one list. `roadmap_milestone_tick` and
`roadmap_milestone_advance` use the `session_triage` owner check (a human-owned session, not
bypassPermissions). `roadmap_milestone_note` skips that check and records the session as actor. All three
mount only when signalling tools are mounted, like `roadmap_resolve`. Each tool gets an eval
case under the existing session-tool evals. The assistant drawer gets the read tool in the same phase; its
write tools are a later phase.

## 12. KinDash migration (reference fixture)

Done when everything in `roadmap.html` is represented:

| roadmap.html element | ViberPit representation |
|---|---|
| Four phase cards, who, chips | `milestones[]` `name`/`label`/`audience`/`platforms` (Kiosk "not yet" = `off`) |
| Gate lists | `gate[]` conditions (mapping below) |
| Checklist ✔ / ◐ / ☐ | leaves (done / in flight / not started) of #20191–#20194, plus `refs`. The curated prose lines ("Foundation: research, inventory…") are dropped by design; the leaves replace them |
| Leaf-count bars | the leaves bar |
| "You are here" | `state = current` on `household` |
| Status lines | `status_note` |
| Numbers panel | `numbers[]`: rubric gap (`ci_metric`), phase leaves (`ladder_leaves`), outside families (`manual`), defects-found-by-CI % (`manual` until KinDash's metrics workflow publishes it) |
| Owner decisions locked | `decision` notes D1–D4, D6, D8, D14, D15 (`locked`); D5, D7, D9–D13 per spec status |
| Needs the owner next | `ask` notes: #20271 sign-off (milestone `community`), `phase1_households`, `phase2_community` |

Gate condition mapping:
- **Phase 0**
  - rubric → `ci_metric rubric_missing_live eq 0`
  - nine lane fixes → `refs_closed` (the nine issue numbers)
  - Nightly → `workflow_streak nightly.yml target 3`
  - consent + DPAs → `refs_closed [#20234, #19215, #19230]`
- **Phase 1**
  - setup with no support call → `manual`
  - week-two activity → `manual`
  - zero P0 → `issue_count`
  - no critical bug in 7 days → `issue_count` with `created:>={now-7d}`
  - cohort bugs closed with tests → `issue_count 'label:"cohort:phase-1" is:open' eq 0`, with a note that
    the regression-test half is enforced by KinDash's guard #20236
- **Phase 2**
  - the four D7 metrics → `manual`, because their inputs are Stripe and Play exports
  - zero P0 → `issue_count`
- **Phase 3**: all three → `manual`

Steps (KinDash repo PRs, after ViberPit phase 2):
1. Add `docs/product/milestones.yaml`.
2. Add `.github/workflows/milestone-metrics.yml`. It runs the rubric lint `--report`, counts
   `required missing` cells for docs whose frontmatter says `state: live`, and uploads `viberpit-metrics`.
3. Seed the notes with `roadmap_milestone_note` calls from a session (no cards, per M8).
4. Once the view is at parity, delete `roadmap.html`, `roadmap.pdf`, `render-roadmap.sh` and the README,
   and replace the core.md "Launch readiness roadmap visual" rule with "update milestones through the
   `roadmap_milestone_*` tools".
5. Fix the stale spec path the old HTML cites.

The ViberPit test suite carries the KinDash YAML as a **fixture** under `server/tests/fixtures/`. No
KinDash name appears in non-test code.

## 13. Non-goals

- Dates, target dates, Gantt or timeline views, "late" alerts (M5).
- ViberPit running product code or repo commands (M3).
- Editing the definition from the UI or MCP. The definition changes only by a repo PR.
- Multiple concurrent ladders per product.
- Creating or editing GitHub epics or issues from the milestone view (GitHub stays the source of truth;
  ViberPit only overlays).
- Persisting or syncing PDFs; re-rendering on status change; copying to a desktop.
- Auto-advance without an actor. A green gate is shown as "ready to advance", and an actor still advances.
- Notifications or push when a gate turns green (later, via the existing escalation path, if wanted).

## 14. Delivery

Each step is its own PR.

1. **This spec.** Stop for owner review.
2. **Phased plan** (`docs/superpowers/plans/2026-11-xx-release-milestones.md`).
3. **Phase 1 vertical slice:**
   - models plus revision;
   - YAML parser and pm-sync apply;
   - the measure registry for `sub_issues`, `issue_count`, `refs_closed`, `workflow_streak`, `manual`
     (read only);
   - the refresh loop and on-demand refresh;
   - `GET` API;
   - a read-only Milestones tab rendering the KinDash fixture against live GitHub.
   It goes through the full four-step server gate and the web gates.
4. Phase 2: `ci_metric` plus the metrics contract doc; write API (manual ticks, notes, status line,
   advance with override); actions in the UI; needs-you integration.
5. Phase 3: MCP tools plus evals; the assistant read tool.
6. Phase 4: print stylesheet plus the one-page e2e.
7. Phase 5: KinDash migration PRs (in KinDash), then retiring the HTML pipeline.

## 15. Left to the builder

- Exact CSS and component split. Reuse existing roadmap and ProductDetail primitives and tokens.
- Whether the partial unique index is portable, or the lock alone suffices on SQLite.
- The GraphQL batching shape and the refresh loop's backoff on budget exhaustion.
- Compact text formatting of `roadmap_milestones` output (kept short; eval-checked).
