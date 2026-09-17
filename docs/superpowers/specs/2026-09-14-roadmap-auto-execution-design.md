# Roadmap auto-execution under a risk tolerance — design

**Status:** approved in outline 2026-09-14 (Tony: approach 1, three named tolerance levels with
risk classes in code, materialization accept is medium risk, initiative priority is derived from
RICE, front-chain cap defaults to 2). Decisions marked **[decided]** were confirmed by Tony;
those marked **[default]** were drafted by the author under the approved outline.

**Principle (Tony, 2026-09-14):** *idle time waiting on a user is a failure mode.* ViberPit keeps
work moving on the user's behalf under a stated risk tolerance instead of waiting for them to
disposition items in the backlog or roadmap. Act-then-notify over ask-then-wait, for anything
reversible.

**Extends:** `2026-09-12-product-automation-policy-design.md` (the per-product policy blob and its
D1/D3/D6 patterns), `2026-08-29-ai-native-sdlc-design.md` §3.3 (the propose-only v1 gate this
spec retires), `specs/003-gh-projects-integration/spec.md` §7b (materialization),
`2026-09-07-roadmap-drilldown-design.md` §11 (attention tiers). Tracks the follow-on the
automation-policy spec named "materialization under the policy (option c)".

## 1. Problem

The Backlog Runner already executes issues in RICE order for any product whose policy says
`dispatch: auto`, and triage proposals self-accept above a confidence tier. But the *front* of
the roadmap, where an initiative becomes a spec, a plan and an epic, stops at every stage and
waits for a human tap, by construction:

- An artifact PR (intent, spec, plan) is merged by a human on GitHub. Until it merges the stage
  engine returns `AwaitApproval`.
- Once merged, the engine still returns `AwaitApproval`: its auto-advance seam
  (`server/sdlc/engine.py:230`, `config.get("propose_only", True)`) has no writer anywhere. It
  is dead code that reads as a feature.
- A plan decomposition is a `MaterializationProposal` that a human accepts or rejects.
- A build-stage initiative whose epic is fully closed shows the `advance` reason in the roadmap's
  needs-you tier until a human clicks Advance.
- A session asking for a more expensive model parks in `pending_model` until a human approves.

Each of these is a *reversible* decision the roadmap surfaces as "needs you". Meanwhile the
synthesizer walks every non-done initiative in table order with no cap, so simply flipping the
dead seam on would fan every initiative out at once, in no particular order. And initiatives
carry no priority at all: only leaf issues have RICE, and issues created by materialization are
unscored, so a freshly approved plan's tasks rank *last* in the queue.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **[decided] Every point where the roadmap waits on a human is a named gate with a risk class, in one registry** (`server/sdlc/gates.py`). A gate auto-resolves when the product's tolerance covers its class; otherwise it parks exactly as today. | One inventory of "where do we wait", one rule for "do we wait". Adding a gate means adding a row, not a boolean on another automation. |
| D2 | **[decided] The dial is one per-product `risk_tolerance` with three levels: `cautious` (today), `balanced` (low-risk gates auto), `bold` (low and medium).** Risk classes live in code beside the gate, not in the policy. `high` never auto-resolves at any level. | A product owner answers "how bold" once; the engineering judgement of what is reversible stays reviewable in one file. Per-gate toggles are the parallel-config bug shape. |
| D3 | **The dial and the front-chain cap join `PRODUCT_POLICY_SCHEMA`.** `risk_tolerance` (enum, default `cautious`) and `max_initiatives_in_flight` (int, default 2 **[decided]**, min 0). Stored in the existing `Project.automation_policy` blob, edited on the product Overview card by the generic renderer. | Constitution VI: one schema, one editor, one validated reading (`effective_policy`). A product without a policy is `cautious`: behaviour-preserving at the flip. |
| D4 | **An auto-resolved gate calls the same function the human button calls**, stamped `decided_by="auto"`, audited, and recorded on the initiative so the roadmap can show what the machine did with the reverse action inline. | The D6 pattern from the automation-policy spec (`apply_accept`). No second write path per gate. |
| D5 | **The reverse action is the existing one; there is no new undo mechanism.** Advance is reversed by Step back; a merged artifact by a revert; an accepted materialization by closing the issues; a model upgrade by switching back. The roadmap shows the chip "auto · <what> · <reverse>" for `AUTO_RESOLVED_VISIBLE_S` (24 h). | Every gate already has a human reverse; adding an undo table would be a second truth about the same state. |
| D6 | **[decided] Initiative priority is derived, never stored:** the epic issue's own RICE score when it is classified, else the highest score among its open sub-issues, else `None`. Materialization seeds each new sub-issue's RICE from the initiative's *source issue* (`origin_ref`, the intake or adopted issue) when that issue is mirrored and fully scored; the epic itself does not exist until the apply creates it. | Evidence the triage layer already produces; no fifth priority vocabulary. Seeding fixes the rank-last inversion for approved plans. |
| D7 | **The synthesizer executes in priority order under a per-product cap.** Initiatives are visited highest priority first (ties: oldest first); new artifact sessions spawn only while the product has fewer than `max_initiatives_in_flight` initiatives *actively producing* an artifact (a live spec/plan session). Gate resolutions (merge, advance, accept) are never capped: they finish work. | "Execute through the roadmap" means one or two things at a time in the order that matters, not everything at once. |
| D8 | **The synthesizer's modes fold to `off | on`; the dead `propose_only` seam is deleted.** Whether a gate auto-resolves is the product's tolerance, not a global mode. | Same fold the runner took in the automation-policy spec (D1/D10). Two dials for one decision is the bug shape named first. |
| D9 | **A process-wide kill switch, `VIBERPIT_GATE_AUTORESOLVE_ENABLED` (default true), forces `cautious` everywhere without touching any policy.** | The C4 lesson (no kill switch for materialize) cost a recovery. One env var, read through `server/config.py`, is cheap insurance. |
| D11 | **[decided] Model capability is a risk mitigation the product owner can buy.** A per-product `unattended_model_floor` (enum `none` or a tier from `MODEL_TIERS`) is the minimum model for any session whose output a covered gate will consume *without a human read*: spec/plan sessions when `artifact.merge` auto-resolves, decompose sessions when `materialization.accept` auto-resolves. Under `cautious` the floor is inert, because a human reads the output. | Raising the tolerance removes a human review; a stronger model is the substitute the owner chooses explicitly, per product, instead of an implicit tier hidden inside each level. Composes with D9's history floor (take the higher). |
| D10 | **The gate registry is display-truthful:** `GET /api/pm/policy-schema` publishes the gate table (id, label, risk, reverse) beside the schema so the product card can explain what each level unlocks. | The operator should read what `balanced` does from the app, not from this document. |

## 3. Use cases

1. **Let viberpit run.** Tony sets viberpit's tolerance to `bold`. An initiative at `design` gets
   its spec PR opened by the synthesizer; when CI is green GitHub merges it (auto-merge armed by
   the loop); the next tick advances the initiative to `plan` and spawns the plan session; the
   plan merges; the decompose session proposes an epic and sub-issues; the loop accepts the
   proposal, the sub-issues inherit the epic's RICE, the Backlog Runner picks them up in order.
   Tony sees "auto · advanced to plan · Step back" chips on the roadmap and one line on the
   Delivery card: "3 auto-resolved this refresh · 0 waiting on you".
2. **Stay careful on a client repo.** kwgt-cairn stays `cautious`. Nothing changes for it; its
   spec PR waits for a human merge as before.
3. **Balanced for KinDash.** KinDash is `balanced`: artifact PRs merge when green and stages
   advance, but plan decompositions still wait in the roadmap for Tony's accept, and model
   upgrades still park.
4. **Reverse a bad call.** The synthesizer advanced `epic(auth):passkeys` to `plan` on a spec
   Tony disagrees with. The chip's Step back returns it to `design`; the spec is a docs commit
   he reverts; the next tick re-proposes only if the spec artifact is gone.
5. **Stop everything now.** `VIBERPIT_GATE_AUTORESOLVE_ENABLED=false` on the daemon and every
   product reads as `cautious` on the next tick. In-flight sessions finish; no gate resolves.
6. **Read the order.** The roadmap's initiative rows show their derived priority; the
   Delivery → SDLC Synthesizer card shows per product "2/2 in front chain · next: <slug>".

## 4. Gates

`server/sdlc/gates.py` (pure, no I/O):

```python
RISK_CLASSES = ("low", "medium", "high")
RISK_TOLERANCES = ("cautious", "balanced", "bold")
_CEILING = {"cautious": None, "balanced": "low", "bold": "medium"}   # high is never auto

@dataclass(frozen=True)
class GateSpec:
    id: str          # "artifact.merge"
    label: str       # "Merge the stage artifact PR"
    risk: str        # RISK_CLASSES
    reverse: str     # "Revert the docs commit"
    action: str      # what auto does, one sentence, shown in the UI

GATES: tuple[GateSpec, ...]
def auto_resolves(gate_id: str, tolerance: str) -> bool
def effective_tolerance(policy_value: str, *, enabled: bool) -> str   # kill switch → cautious
```

| id | risk | today | auto action | reverse |
|---|---|---|---|---|
| `artifact.merge` | low | human merges the intent/spec/plan PR | arm GitHub auto-merge (squash) on the artifact PR; CI stays the gate | revert the docs commit |
| `stage.advance` | low | Advance tap after the artifact merged | `advance_stage`, same write as `POST /advance` | Step back |
| `stage.advance_built` | low | Advance tap when the build epic is fully closed (the `advance` reason) | advance `build → deploy` | Step back |
| `materialization.accept` | medium **[decided]** | Accept tap on the decomposition proposal | `sdlc_materialize_apply(proposal)` through the shared accept function | close the created issues (the roadmap's existing dispositions) |
| `model.upgrade` | medium | approval card on a parked upgrade | `resolve_model_approval(..., "approve", identity="auto")` at the requested tier, only after the existing self-switch guards passed | switch the model back |
| `dispatch` | (policy) | `dispatch: auto` already exists | unchanged, listed for completeness | archive the session |
| `triage.accept` | (policy) | `triage_auto_accept` already exists | unchanged | undo within the window |
| `issue.needs_spec`, `session.hand_back`, `initiative.blocked`, `triage.no_fit`, `deploy.production`, `issue.close` | high | human | **never** | not cleanly |

`dispatch` and `triage.accept` keep their own knobs (already shipped, finer-grained than a
tolerance level); they appear in the registry with `risk=None` so the UI table is complete and
so nobody adds a second copy of them. **[default]**

**Deploy and beyond.** `stage.advance_built` moves a finished initiative from needs-you to
in-flight at `deploy`; the deploy, maintain and done transitions stay human (production deploy
is `high`). The synthesizer's `_enter_stage("deploy")` keeps returning `NeedsHuman` and the row
reads "built, awaiting deploy" rather than "advance". **[default]** Automating that is
deploy-monitor's follow-on, not this spec's.

## 5. Engine

`next_action(initiative, artifacts, mode, tolerance)` replaces the `config` parameter: the only
key it ever read was `propose_only`, which is deleted (D8). New pure actions:

- `MergeArtifact(kind, pr_number)`: the stage artifact PR is open and `artifact.merge` auto-resolves
  under *tolerance*. Otherwise `AwaitApproval(kind)` exactly as today.
- `Advance(to_stage)`: the artifact is merged (and, at `plan`, the epic exists) and `stage.advance`
  auto-resolves. Otherwise `AwaitApproval(kind)`.
- At `build`: `InitiativeView` gains `epic_total: int = 0`, `epic_done: int = 0`. When
  `epic_total > 0 and epic_done >= epic_total` and `stage.advance_built` auto-resolves →
  `Advance("deploy")`; otherwise today's `HandoffToBuild` / `NeedsHuman`.

`mode` keeps only `off | on` (`off` → `NeedsHuman("SDLC automation is off")`, anything else
proceeds); the synthesizer's stored `propose`/`auto` folds to `on` through one
`fold_synth_mode`, shared by its read path and a one-shot row rewrite on the first tick, the
way the runner's `fold_global_mode` does. SDLC Intake keeps its own modes and only ever
proposes intents, so it calls `next_action` with `tolerance="cautious"`; the synthesizer is the
one loop that resolves gates, for every artifact kind including intent, exactly as it already
owns enrichment for every kind. The engine stays a total function over plain views: the
tolerance is a string it is handed, never something it looks up.

## 6. The synthesizer loop

One tick (`SdlcSynthesizerLoop._run`), in order:

1. `read_config()` → `(mode, config)`; `off` returns. Load policies once
   (`policies_by_slug`), and the kill switch, into `tolerance_by_project`.
2. `list_initiatives()` now returns leaf initiatives **with** `project_id`, `priority`,
   `epic_total`, `epic_done`, and is **sorted** by `(priority is None, -priority, created_at)`
   (D6/D7). Priority is computed in the loader with the batched epic-progress idiom the
   initiatives route already uses (`_epic_progress_batch`), never per row.
3. For each initiative, `next_action(view, arts, mode, tolerance)`:
   - `Propose(kind)`: spawn only if the product's *producing* count is below
     `max_initiatives_in_flight`, where producing = the initiative's CURRENT stage is `design`
     or `plan`, that stage's artifact row is unmerged (`commit_sha is None`) **and its artifact
     session is live**. Intake never counts (its own daily budget governs it), a `blocked`
     initiative never counts, a session that died without a PR never leaks a slot, and an open
     artifact PR awaiting a human merge does not count: under `cautious` the cap bounds
     concurrent machine work, never the human merge queue. **[default, amended after the phase 1
     whole-branch review]** Over cap → count as `queued`, no spawn, no status write, plus one
     `ceiling` activity event per product per tick so the stall is visible.
   - `MergeArtifact`: call the new seam `arm_auto_merge(repo, pr_number)` once; stamp
     `InitiativeArtifact.auto_merge_armed_at` (new nullable column) so the tick never re-runs
     `gh` for the same PR. Record the gate (§8).
   - `Advance(to)`: `set_stage(id, to)` through the same seam `POST /advance` uses; status
     `active`; record the gate.
   - `Materialize()`: unchanged spawn path. Then, **new**, when a pending
     `MaterializationProposal` exists and `materialization.accept` auto-resolves: call the shared
     accept function (§7) and record the gate. Never when any `MaterializationProposal` for this
     initiative has `status == "rejected"`: a human rejected a decomposition of this plan once,
     and the machine does not get a second vote (the `auto_undone` rule from triage). Clearing
     the rejected proposals, which is already how a human re-arms decomposition attempts,
     re-arms auto-accept too. **[default]**
   - `AwaitApproval` / `NeedsHuman`: counted as `awaiting` as today.
4. `_enrich(artifacts)` unchanged; it is what turns an armed PR into `commit_sha` on merge.
5. Publish counts: `{initiatives, proposed, awaiting, queued, auto_resolved}` plus per-product
   `{slug, tolerance, producing, cap, next}`.

**Arming auto-merge.** `arm_auto_merge` is a `gitrepos._run_gh` call (`gh pr merge --auto
--squash <n> --repo <repo>`), the same subprocess seam `server/pm/map_changes_gh.py` uses for its
PR lifecycle. GitHub applies the repo's branch protection: required checks must pass and the
branch must be up to date, so "auto" is bounded by CI. If the repo has auto-merge disabled or no
protection, `gh` refuses; the loop records `gate_refused` with GitHub's message, leaves the
artifact at `AwaitApproval`, and the roadmap chip says "auto-merge unavailable on this repo".
**[default]** No direct `--merge` fallback: merging with no CI at all is not low risk. Principle
III: the exact refusal wording is verified live in the plan's first task.

The artifact prompt's line 4 ("Do NOT arm auto-merge") stays, reworded: the *loop* is the one
writer that arms it, so an artifact session never does.

## 7. Shared accept function for materialization

`server/pm/materialize_accept.py`: `accept_materialization(db, orch, proposal, *, decided_by,
identity)` moves the body of `POST /initiatives/{slug}/materialization/accept` (apply through
`sdlc_materialize_apply`, audit `sdlc.materialization.accept` with `decided_by`, commit) out of
`server/api/sdlc.py`; the route calls it with `decided_by="human"`, the loop with `"auto"`. Same
re-read-under-lock shape `pending_proposal_stmt` established for triage: a human accept and the
loop must not both apply one proposal.

Materialization seeding (D6): `materialize_apply.py` copies `reach/impact/effort/confidence` onto
each created sub-issue from the `WorkItem` mirroring the initiative's `origin_ref` when all four
are set, `classified_by="materialize"` as today; when there is no source issue or it is unscored
the sub-issues stay unscored (no invented numbers). The new epic's own score arrives through the
normal mirror and triage path.

## 8. Recording an auto-resolution

- `Initiative.last_auto_gate` (JSON, nullable): `{"gate": id, "at": iso, "tolerance": level,
  "detail": {...}}`, overwritten by each resolution. Nullable, so `_reconcile_columns` adds it on
  both dialects; migration `0045_roadmap_auto_execution` recorded anyway, chained off
  `0044_dispatch_outcomes` (re-numbered at implementation time if master has moved on, so two
  branches never share a number).
- Audit `sdlc.gate.auto_resolve` `{initiative, gate, risk, tolerance, detail}`; `AutomationEvent`
  kind `auto_resolved` (and `gate_refused`).
- For `model.upgrade`, the existing `model_approval.approve` audit carries
  `{requested, applied, decided_by: "auto"}`; nothing initiative-scoped, since a session is the
  subject.

## 9. Model upgrade gate

Where a self-directed upgrade would park (`resolve_self_directed_model_policy` → `park`), the
orchestrator resolves the session's product (`owning_projects_by_repo` on the session's repo)
and, if `model.upgrade` auto-resolves under that product's tolerance, calls
`resolve_model_approval(session_id, "approve", identity="auto")` instead of parking. The existing
guards (`evaluate_guards`: same-tier dedupe, `CCSWITCH_SELF_MODEL_SWITCH_CAP`, flip-flop halt,
human-amended drop) run first and are unchanged; a `block` from the tier denylist is still a
block. A session with no owning product parks as today. The kill switch
`CCSWITCH_SELF_MODEL_SWITCH_ENABLED=false` still wins.

## 9b. Unattended model floor (D11)

Where the synthesizer spawns a spec, plan or decompose session (`_sdlc_spawn_synth`,
`_sdlc_spawn_materialize`), it resolves the model through the existing `resolve_model(config,
summary, floor=…)` with

```
floor = highest_tier(history_floor, unattended_floor)
unattended_floor = policy["unattended_model_floor"] if the gate that will consume this
                   output auto-resolves under the product's effective tolerance else None
```

- The consuming gate is `artifact.merge` for spec/plan sessions and `materialization.accept`
  for decompose sessions. Under `cautious`, or with the kill switch off, `unattended_floor` is
  `None` and nothing changes.
- Unlike D9's history floor, the unattended floor raises a **pinned** lower `default_model`
  too: the owner set both knobs, and "this output ships unread" is the stricter statement.
- The floor is capped by the model-approval policy: a floor tier the denylist would `block` or
  park for approval (`resolve_model_policy` ≠ `allow`) is lowered to the highest allowed tier,
  because a parked launch is the idle wait this spec exists to remove. Audit `sdlc.spawn`
  detail carries `{assessed, pinned, history_floor, unattended_floor, applied, capped}`.
- Backlog Runner dispatches are not covered: a coding session's output is a PR that CI and the
  merge gate review, and D9 already floors it from evidence. **[default]**

## 10. Priority

`server/pm/priority.py`: `initiative_priority(epic_score: float | None, open_leaf_scores:
Iterable[float]) -> float | None`: the epic's own score when present, else `max` of the open
leaves' scores, else `None`. One pure function; both the synthesizer loader and the roadmap tree
loader call it (constitution VI). Exposed as `priority` on `GET /api/sdlc/initiatives` rows and
on the roadmap `InitiativeNode`; programs derive theirs as the max over phases. The web renders
it as a muted number on the initiative row and uses it as the secondary sort inside a tier.
Nothing is stored.

## 11. UI

- **Product Overview → Automation card:** the two new fields render through the existing generic
  `ConfigInput`. Under the tolerance select, a compact table from `GET /api/pm/policy-schema`
  (D10): each gate's label, risk, and whether the selected level auto-resolves it. (Built 2026-09-17 from `GET /api/sdlc/gates` rather than the policy schema; see `2026-09-17-roadmap-consequence-guidance-design.md` D5.)
- **Roadmap initiative rows and page:** chip `auto · advanced to plan` / `auto · merged spec` /
  `auto · accepted decomposition` for 24 h after `last_auto_gate.at`, with the reverse action
  inline where one exists in the app (Step back). `gate_refused` renders as
  `auto-merge unavailable` in the needs-you reason slot.
- **Delivery → SDLC Synthesizer card:** "N auto-resolved this refresh · M waiting on you ·
  K queued behind the cap"; per-product lines `slug · tolerance · producing/cap · next: <slug>`.
  `waiting_reason="human"` only when M > 0.
- **Roadmap needs-you list:** unchanged; auto-resolved rows leave it, which is the point.

## 12. Data model and API summary

- `PRODUCT_POLICY_SCHEMA` + `risk_tolerance` (enum `cautious | balanced | bold`, default
  `cautious`) + `max_initiatives_in_flight` (int, default 2, min 0) + `unattended_model_floor`
  (enum `none | haiku | sonnet | opus | fable`, choices from `MODEL_TIERS`, default `none`).
- `Initiative.last_auto_gate` JSON nullable; `InitiativeArtifact.auto_merge_armed_at` datetime
  nullable. Migration recorded; reconcile applies.
- `GET /api/pm/policy-schema` gains `gates: [{id, label, risk, action, reverse}]`.
- `GET /api/sdlc/initiatives` and `GET /api/pm/map` initiative nodes gain `priority` and
  `autoResolved` (`{gate, at, reverse}` or `null`, only inside the visible window).
- SDLC Synthesizer status payload gains `queued`, `autoResolved`, `products[]`.
- Synthesizer modes `off | on` (fold); `propose_only` gone; `VIBERPIT_GATE_AUTORESOLVE_ENABLED`
  in `server/config.py`.

## 13. Phasing

1. **Substrate, behaviour-preserving at the flip.** `gates.py`, the three policy keys, the engine
   signature change with `MergeArtifact`/`Advance` actions, mode fold and `propose_only`
   deletion, `priority.py` + sorted loader + cap counting + `queued` count, RICE seeding in
   materialize, `priority` in the API/map, the kill switch. With every product `cautious`
   nothing auto-resolves; the only visible change is order, the cap, and priority numbers.
2. **Low-risk gates.** `arm_auto_merge` seam (live-verified), `auto_merge_armed_at`,
   `stage.advance` and `stage.advance_built` execution, `last_auto_gate`, audits/events, the
   roadmap chip, the Synthesizer card lines, the gate table on the product card, and the
   unattended model floor on spec/plan spawns (§9b).
3. **Medium-risk gates.** `materialize_accept.py` extraction and the loop's accept step with the
   rejected-before rule; the `model.upgrade` path in the orchestrator; the unattended floor on
   decompose spawns.

Not in this spec (follow-ons): deploy/maintain/done automation, an LLM disposition agent for the
`high` gates (approach 3 in the brainstorm, a later `bold` extension), per-product notification
digests, a manual priority override on initiatives.

## 14. Testing

- Pure: `auto_resolves` over the full level × class matrix incl. `high` never and the kill
  switch; `next_action` cases per stage × tolerance (the existing engine table gains a tolerance
  axis); `initiative_priority`; the producing/cap rule; `fold_global_mode` for the synthesizer.
- Loop (`test_sdlc_synthesizer_loop.py`, fakes): sorted visiting order; cap blocks a third
  spawn but never a merge/advance; `MergeArtifact` arms once and stamps; refusal records
  `gate_refused` and keeps `AwaitApproval`; `Advance` writes stage + `last_auto_gate`;
  materialization auto-accept happens once, never while a rejected proposal exists for the
  initiative, never at `balanced`.
- Orchestrator seams: `arm_auto_merge` argv shape against a fake `_run_gh`; the model-upgrade
  path approves under `bold`, parks under `balanced`/no product/kill switch; the unattended
  floor raises `auto` and a pinned lower alias under `balanced`, is inert under `cautious`,
  takes the higher of itself and the history floor, and is lowered to the highest allowed tier
  when the denylist would park it.
- API: policy PATCH accepts the new keys and rejects garbage; `policy-schema` carries the gate
  table; the accept route still audits `decided_by="human"`; initiatives carry `priority`.
- Postgres suite for the two new nullable columns (no `_REQUIRED_EXTRAS` change).
- Web: card renders the gate table per level; chip appears inside the window with Step back
  wired, disappears after; initiative row shows priority. Playwright: set `balanced` on a product
  and see the gate table flip.
- Assistant route allowlist: no new mutating route is added for the assistant; the
  `policy-schema` read is already allowed.

## 15. Failure modes

- **Auto-merge refused by GitHub** (setting off, no protection, PR not mergeable): recorded once
  as `gate_refused`, artifact stays awaiting, chip explains; the tick does not retry `gh` until
  the PR's head sha changes.
- **Advance races a human Step back:** the advance seam re-reads the row and writes only if the
  stage is still the one the engine decided from (compare-and-set), so a human's newer decision
  wins; the loop logs `stale` and moves on.
- **Materialization accept races a human reject:** the shared accept re-reads under lock; the
  loser backs out (the route answers 409 `already_resolved`, the loop answers `None`).
- **Priority ties everywhere** (a product with no RICE at all): order falls back to age, the cap
  still applies; the card's `next:` shows the oldest.
- **Policy row corrupt:** `effective_policy` already falls back to defaults, which are
  `cautious` and cap 2, so a bad row can only make a product *more* careful.
- **Kill switch flipped mid-tick:** takes effect on the next tick; nothing in-flight is
  interrupted (a merge GitHub already performed is a merge).
- **Tolerance lowered after a gate armed auto-merge:** the PR still merges when green; lowering
  the dial is not a revert. The chip and audit make that visible; disarming requires
  `gh pr merge --disable-auto` by hand. **[default]**

## 16. Decisions of record (2026-09-14)

Answered by Tony on 2026-09-14: (1) approach 1, the gate registry plus one tolerance dial;
(2) three named levels with risk classes in code, not per-gate toggles; (3) materialization
accept is medium risk (`bold` only); (4) initiative priority is derived from RICE, no manual
field; (5) `max_initiatives_in_flight` defaults to 2; (6) model capability is an explicit risk
mitigation, added as the per-product `unattended_model_floor` (D11, §9b) after Tony's follow-up
"we could also allow choosing model type as a way to mitigate risk". Drafted under that outline and open to
objection: the deploy-and-beyond boundary (§4), no direct-merge fallback when GitHub refuses
auto-merge (§6), the rejected-before rule for materialization (§6), the 24 h chip window (D5),
and the lowered-dial semantics (§15).
